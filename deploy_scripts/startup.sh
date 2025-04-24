#!/bin/bash
#
# Main startup script for the application deployment.
# Assumes dependencies are installed by install_deps.sh and runs as 'ccuser'.
# Requires PROD_* secret environment variables to be set.
#
set -e

# --- Configuration & Logging ---
LOG_DIR="/local/logs"
STARTUP_LOG="$LOG_DIR/startup.log"
TUNNEL_LOG="$LOG_DIR/tunnel.log"
SOCAT_80_LOG="$LOG_DIR/socat_80.log"
SOCAT_443_LOG="$LOG_DIR/socat_443.log" 
KEEL_SOCAT_LOG="$LOG_DIR/keel_socat.log"
REPO_DIR="/local/repository"
HELM_DIR="$REPO_DIR/helm"

# Redirect all script output (stdout & stderr) to the startup log file
exec > >(tee -a "$STARTUP_LOG") 2>&1

echo "--- Startup Script Started: $(date) ---"
echo "User: $(whoami)"
echo "Current Directory: $(pwd)"
echo "Initial PATH: $PATH"

# --- Validate Secrets ---
if [ -z "$PROD_ENCRYPTION_KEY" ] || [ -z "$PROD_REDIS_PASSWORD" ] || [ -z "$PROD_SESSION_SECRET" ]; then
  echo "ERROR: Missing one or more required secret environment variables (PROD_ENCRYPTION_KEY, PROD_REDIS_PASSWORD, PROD_SESSION_SECRET)."
  exit 1
fi
echo "Required secrets validated."

# --- Minikube Initialization ---
echo "Starting Minikube cluster (driver: docker)..."
minikube start --driver=docker

echo "Enabling Minikube Ingress addon..."
minikube addons enable ingress

echo "Patching ingress-nginx service type to LoadBalancer..."
# This allows external access via the Minikube tunnel IP
kubectl patch svc ingress-nginx-controller \
  -n ingress-nginx \
  -p '{"spec": {"type": "LoadBalancer"}}' \

echo "Waiting for ingress-nginx controller pod to become ready..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=120s

# --- Network Forwarding ---
echo "Starting Minikube tunnel in background (logs to $TUNNEL_LOG)..."
# Requires passwordless sudo for 'ccuser' (configured in install_deps.sh)
nohup sudo minikube tunnel > "$TUNNEL_LOG" 2>&1 &
# Allow a few seconds for the tunnel process to establish
sleep 5

echo "Starting socat port forward (80 -> 192.168.49.2:80) in background (logs to $SOCAT_80_LOG)..."
# Forwards host port 80 to the Minikube Ingress service IP (typically 192.168.49.2)
setsid sudo socat TCP-LISTEN:80,fork TCP:192.168.49.2:80 </dev/null &>> "$SOCAT_80_LOG" &

echo "Starting socat port forward (443 -> $MINIKUBE_IP:443) in background (logs to $SOCAT_443_LOG)..." # Added for HTTPS
setsid sudo socat TCP-LISTEN:443,fork TCP:$MINIKUBE_IP:443 </dev/null &>> "$SOCAT_443_LOG" & # Added for HTTPS

TLS_SECRET_NAME="campusconnect-tls" # Define secret name
NAMESPACE="default"
TEMP_TLS_KEY_PATH="$TMPDIR/tls.key" # Use TMPDIR for temp files
TEMP_TLS_CERT_PATH="$TMPDIR/tls.crt"
TLS_SETUP_DONE=false # Flag to track if TLS was configured

# Clean up potential leftover temp files first
rm -f "$TEMP_TLS_KEY_PATH" "$TEMP_TLS_CERT_PATH"

if [ -n "$TLS_KEY_B64" ] && [ -n "$TLS_CERT_B64" ]; then
    echo "TLS_KEY_B64 and TLS_CERT_B64 environment variables found. Decoding and creating Kubernetes TLS secret '$TLS_SECRET_NAME'..."

    # Decode Base64 key and cert into temporary files
    echo "Decoding Base64 key to $TEMP_TLS_KEY_PATH..."
    echo "$TLS_KEY_B64" | base64 --decode > "$TEMP_TLS_KEY_PATH"
    if [ $? -ne 0 ]; then echo "ERROR: Failed to decode Base64 TLS key."; rm -f "$TEMP_TLS_KEY_PATH"; exit 1; fi

    echo "Decoding Base64 certificate to $TEMP_TLS_CERT_PATH..."
    echo "$TLS_CERT_B64" | base64 --decode > "$TEMP_TLS_CERT_PATH"
    if [ $? -ne 0 ]; then echo "ERROR: Failed to decode Base64 TLS certificate."; rm -f "$TEMP_TLS_KEY_PATH" "$TEMP_TLS_CERT_PATH"; exit 1; fi

    # Set secure permissions for the temporary files
    chmod 600 "$TEMP_TLS_KEY_PATH" "$TEMP_TLS_CERT_PATH"
    echo "Temporary TLS files created and secured."

    # Delete existing secret first (ignore if not found)
    echo "Deleting existing secret '$TLS_SECRET_NAME' if present..."
    kubectl delete secret $TLS_SECRET_NAME -n $NAMESPACE --ignore-not-found=true --timeout=60s

    # Create the TLS secret from the temporary files
    echo "Creating new secret '$TLS_SECRET_NAME' using temporary files..."
    kubectl create secret tls $TLS_SECRET_NAME \
        --key "$TEMP_TLS_KEY_PATH" \
        --cert "$TEMP_TLS_CERT_PATH" \
        -n $NAMESPACE

    # Securely delete the temporary files immediately after use
    echo "Cleaning up temporary TLS key and certificate files..."
    rm -f "$TEMP_TLS_KEY_PATH" "$TEMP_TLS_CERT_PATH"
    echo "Temporary TLS files removed."

    # Verify secret creation
    if ! kubectl get secret $TLS_SECRET_NAME -n $NAMESPACE > /dev/null; then
        echo "ERROR: Failed to create or verify Kubernetes TLS secret '$TLS_SECRET_NAME'."
        exit 1 # Exit if TLS secret creation fails
    else
        echo "Kubernetes TLS secret '$TLS_SECRET_NAME' created successfully."
        TLS_SETUP_DONE=true # Mark TLS as configured
    fi
else
    echo "TLS_KEY_B64 or TLS_CERT_B64 environment variables not set, skipping Kubernetes TLS secret creation."
fi

# --- Docker Environment ---
echo "Configuring shell to use Minikube's Docker daemon..."
eval $(minikube docker-env)
# Verify docker context points to minikube
docker info | grep -i "kubernetes.*minikube" || echo "WARNING: Docker context might not be set to Minikube."

# --- Temporary Directory ---
# Ensure TMPDIR is set and writable (should be configured by install_deps.sh)
export TMPDIR=/var/tmp/ccuser-tmp
if [ ! -d "$TMPDIR" ] || ! touch "$TMPDIR/.writable_test" 2>/dev/null; then
    echo "ERROR: TMPDIR ($TMPDIR) is not writable or does not exist. Check install_deps.sh."
    # Attempt recovery (permissions might be wrong)
    sudo mkdir -p "$TMPDIR" && sudo chown "$(whoami):$(whoami)" "$TMPDIR" && sudo chmod 1777 "$TMPDIR"
    if ! touch "$TMPDIR/.writable_test" 2>/dev/null; then
        echo "ERROR: Failed to fix TMPDIR permissions."
        exit 1
    fi
fi
rm -f "$TMPDIR/.writable_test"
echo "TMPDIR configured: $TMPDIR"

# --- Keel Installation (via Helm) ---
echo "Installing/Updating Keel webhook via Helm..."
helm repo add keel https://charts.keel.sh >> "$STARTUP_LOG" 2>&1 # Add repo quietly
helm repo update >> "$STARTUP_LOG" 2>&1 # Update quietly

cd "$HELM_DIR" || { echo "ERROR: Failed to cd into Helm directory: $HELM_DIR"; exit 1; }

echo "Running 'helm upgrade --install keel'..."
# Use helm upgrade --install for idempotency
helm upgrade --install keel keel/keel \
    -n default \
    -f keel-values.yaml \
    --timeout 5m # Add timeout to helm operation
echo "Keel Helm operation finished."

echo "Waiting for Keel deployment to become available (max 5 minutes)..."
# Wait loop to check Keel deployment status
attempts=0
max_attempts=30 # 30 attempts * 10 seconds = 300 seconds
deployment_name="keel"
namespace="default"

while [ $attempts -lt $max_attempts ]; do
    # Check the 'Available' condition status
    status=$(kubectl get deployment $deployment_name -n $namespace -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)

    if [ "$status" == "True" ]; then
        echo "Keel deployment '$deployment_name' in namespace '$namespace' is Available."
        break # Success
    fi

    # Log current replica status for progress indication
    replicas=$(kubectl get deployment $deployment_name -n $namespace -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
    target_replicas=$(kubectl get deployment $deployment_name -n $namespace -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")
    echo "Waiting for Keel deployment... Ready Replicas: ${replicas:-0}/${target_replicas:-1} (Attempt $((attempts+1))/$max_attempts)" 2>&1 &

    attempts=$((attempts+1))
    sleep 10
done

# Check if the loop timed out
if [ $attempts -eq $max_attempts ]; then
    echo "ERROR: Keel deployment '$deployment_name' did not become available within the timeout."
    echo "--- Keel Deployment Status ---"
    kubectl get deployment $deployment_name -n $namespace -o yaml || echo "Failed to get deployment YAML."
    echo "--- Keel Pods ---"
    # Assuming 'app=keel' is the correct label selector for Keel pods
    kubectl get pods -n $namespace -l app=keel || echo "Failed to get Keel pods."
    exit 1
fi

# --- Keel Service Forwarding ---
echo "Retrieving Keel service IP..."
# Allow some time for LoadBalancer IP assignment if applicable
sleep 5
SERVICE_IP=$(kubectl get svc -n default keel -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)

# Fallback to ClusterIP if LoadBalancer IP is not available
if [ -z "$SERVICE_IP" ]; then
    echo "Keel LoadBalancer IP not found, attempting to use ClusterIP..."
    SERVICE_IP=$(kubectl get svc -n default keel -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
    if [ -z "$SERVICE_IP" ]; then
        echo "ERROR: Failed to get Keel service IP (LoadBalancer or ClusterIP)."
        kubectl get svc -n default keel -o yaml # Log service details for debugging
        exit 1
    else
       echo "Using Keel ClusterIP: $SERVICE_IP"
    fi
else
    echo "Keel LoadBalancer IP: $SERVICE_IP"
fi

echo "Forwarding port 9300 to Keel service ($SERVICE_IP:9300) in background (logs to $KEEL_SOCAT_LOG)..."
setsid sudo socat TCP-LISTEN:9300,fork TCP:$SERVICE_IP:9300 </dev/null >> "$KEEL_SOCAT_LOG" 2>&1 &

# --- Application Secrets ---
SECRET_NAME="campus-connect-config-secrets"
NAMESPACE="default"
echo "Creating/Updating Kubernetes secret '$SECRET_NAME' in namespace '$NAMESPACE'..."

# Ensure clean state by deleting existing secret first (ignore if not found)
kubectl delete secret $SECRET_NAME -n $NAMESPACE --ignore-not-found=true --timeout=60s

# Create the secret using values from environment variables
kubectl create secret generic $SECRET_NAME -n $NAMESPACE \
  --from-literal=ENCRYPTION_KEY="$PROD_ENCRYPTION_KEY" \
  --from-literal=REDIS_PASSWORD="$PROD_REDIS_PASSWORD" \
  --from-literal=SESSION_SECRET="$PROD_SESSION_SECRET"

# Verify secret creation
if ! kubectl get secret $SECRET_NAME -n $NAMESPACE > /dev/null; then
  echo "ERROR: Failed to create or verify Kubernetes secret '$SECRET_NAME'."
  exit 1
fi
echo "Kubernetes secret '$SECRET_NAME' created successfully."

# --- Skaffold Deployment ---
echo "Changing to repository directory: $REPO_DIR"
cd "$REPO_DIR" || { echo "ERROR: Failed to cd into repository directory: $REPO_DIR"; exit 1; }

echo "Deploying application using Skaffold (profile: prod-deploy)..."
# Run skaffold deploy with 'info' verbosity; output is already redirected to the main log
skaffold deploy -p prod-deploy -v info

# --- Final Output ---
HOSTNAME=$(hostname -f) # Get the fully qualified domain name

echo ""
echo "--- Deployment Process Complete ---"
echo "Application should be accessible at: http://$HOSTNAME"
echo "(Note: If $HOSTNAME is not resolvable externally, manual DNS or /etc/hosts configuration may be required on your client machine.)"
echo ""
echo "Startup script finished successfully at $(date)"