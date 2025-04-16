#!/bin/bash
set -e

# Define LOG path early
LOG="/local/logs/startup.log"

# Restore complex redirection
exec > >(tee -a "$LOG") 2>&1

echo "Startup script started at $(date)"
echo "Current PATH: $PATH" # Log the PATH as seen by the script

echo "🚀 Starting Minikube..."
minikube start --driver=docker

echo "🔌 Enabling Ingress..."
minikube addons enable ingress

echo "🔧 Patching ingress-nginx service to LoadBalancer..."
kubectl patch svc ingress-nginx-controller \
  -n ingress-nginx \
  -p '{"spec": {"type": "LoadBalancer"}}'

echo "⏱ Waiting for ingress-nginx-controller to be ready..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=90s

echo "🌉 Starting Minikube tunnel..."
# Rely on NOPASSWD for ccuser
nohup sudo minikube tunnel > /local/logs/tunnel.log 2>&1 &

echo "🔁 Starting socat port forward from 80 -> 192.168.49.2:80..."
# Rely on NOPASSWD for ccuser
nohup sudo socat TCP-LISTEN:80,fork TCP:192.168.49.2:80 > /local/logs/socat.log 2>&1 &

echo "🐳 Configuring Docker to use Minikube's Docker daemon..."
eval $(minikube docker-env)

export TMPDIR=/var/tmp/ccuser-tmp
# Permissions should be okay from install_deps.sh
# sudo chown -R ccuser:ccuser /local/
# sudo chmod -R 775 /local/

echo "📦 Installing Keel separately via Helm..."
# Redirect stdout and stderr of helm repo commands to the log file
helm repo add keel https://charts.keel.sh >> "$LOG" 2>&1
helm repo update >> "$LOG" 2>&1
cd /local/repository/helm
echo "Running helm upgrade for Keel..." # Add log before helm command
# Redirect stdout and stderr of helm upgrade command to the log file
helm upgrade --install keel keel/keel -n default -f keel-values.yaml >> "$LOG" 2>&1
echo "Helm upgrade command finished." # Add log after helm command

echo "Waiting for the Keel deployment to become available (max 5 minutes)..."

# --- Replace kubectl rollout status ---
# kubectl rollout status deployment/keel -n default --timeout=5m

# --- Start Replacement Loop ---
attempts=0
max_attempts=30 # 30 attempts * 10 seconds = 300 seconds = 5 minutes
deployment_name="keel"
namespace="default"

while [ $attempts -lt $max_attempts ]; do
    # Check if the 'Available' condition is 'True'
    status=$(kubectl get deployment $deployment_name -n $namespace -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null)

    if [ "$status" == "True" ]; then
        echo "Deployment $deployment_name in namespace $namespace is Available."
        break # Exit the loop successfully
    fi

    # Optional: Check for progressing status to give more feedback
    progressing_status=$(kubectl get deployment $deployment_name -n $namespace -o jsonpath='{.status.conditions[?(@.type=="Progressing")].status}' 2>/dev/null)
    replicas=$(kubectl get deployment $deployment_name -n $namespace -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo "0")
    target_replicas=$(kubectl get deployment $deployment_name -n $namespace -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "1")

    echo "Waiting for Keel deployment... Status: $status, Progressing: $progressing_status, Replicas: ${replicas:-0}/${target_replicas:-1} (Attempt $((attempts+1))/$max_attempts)"
    attempts=$((attempts+1))
    sleep 10
done

if [ $attempts -eq $max_attempts ]; then
    echo "Error: Keel deployment $deployment_name did not become available after $max_attempts attempts."
    # Optional: Dump deployment status for debugging
    kubectl get deployment $deployment_name -n $namespace -o yaml
    kubectl get pods -n $namespace -l app=keel # Assuming 'app=keel' is the correct label
    exit 1 # Exit script due to timeout
fi
# --- End Replacement Loop ---

echo "Keel deployment rollout status command finished."

echo "Proceeding to Skaffold deployment..." # Added log

echo "🚀 Deploying app with Skaffold..."
cd /local/repository

echo "current directory: $(pwd)"
echo "current user: $(whoami)"


echo "DEBUG: Checking secrets before Skaffold deploy:"
echo "PROD_ENCRYPTION_KEY='${PROD_ENCRYPTION_KEY}'"
echo "PROD_REDIS_PASSWORD='${PROD_REDIS_PASSWORD}'"
echo "PROD_SESSION_SECRET='${PROD_SESSION_SECRET}'"

skaffold deploy -p prod-deploy -v debug # Output already redirected by exec

HOSTNAME=$(hostname -f)

echo ""
echo "✅ All done! App should be accessible at: http://$HOSTNAME"
echo ""

# Add final success message
echo "Startup script finished successfully at $(date)"
