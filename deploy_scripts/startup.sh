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
helm repo add keel https://charts.keel.sh
helm repo update
cd /local/repository/helm
helm upgrade --install keel keel/keel -n default -f keel-values.yaml

echo "Waiting for the Keel deployment to become available (max 5 minutes)..."
kubectl rollout status deployment/keel -n default --timeout=5m
echo "Keel deployment rollout status command finished." # Added log

echo "Attempting to get Keel service IP..." # Added log
SERVICE_IP="" # Initialize variable
SERVICE_IP=$(kubectl get svc -n default keel -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
GET_SVC_EXIT_CODE=$? # Capture exit code immediately
echo "kubectl get svc exit code: $GET_SVC_EXIT_CODE" # Added log

if [ $GET_SVC_EXIT_CODE -ne 0 ]; then
    echo "Error: Failed to get Keel service IP. Exit code: $GET_SVC_EXIT_CODE"
    # Optionally dump service details for debugging
    kubectl get svc keel -n default -o yaml
    exit 1 # Explicitly exit if getting IP failed
fi

if [ -z "$SERVICE_IP" ]; then
    echo "Error: Keel service IP is empty even though kubectl command succeeded."
    # Optionally dump service details for debugging
    kubectl get svc keel -n default -o yaml
    exit 1 # Explicitly exit if IP is empty
fi

echo "Keel service IP obtained: $SERVICE_IP" # Added log
echo "Keel is now running and available at http://$SERVICE_IP:9300"

echo "Attempting to forward port 9300 via socat..." # Added log
# Rely on NOPASSWD for ccuser
nohup sudo socat TCP-LISTEN:9300,fork TCP:$SERVICE_IP:9300 > /local/logs/keel.log 2>&1 &
SOCAT_EXIT_CODE=$? # Capture exit code of starting nohup/sudo/socat
echo "nohup sudo socat command exit code: $SOCAT_EXIT_CODE" # Added log
# Note: This only captures the exit code of *launching* the background process.
# If sudo fails immediately, it might be non-zero. If socat fails later, this won't show it.
if [ $SOCAT_EXIT_CODE -ne 0 ]; then
    echo "Warning: Failed to start socat process. Exit code: $SOCAT_EXIT_CODE. Continuing..."
    # Decide if this should be fatal or just a warning
fi

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
