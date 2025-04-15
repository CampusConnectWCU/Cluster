#!/bin/bash
set -e

# Define LOG path early
LOG="/local/logs/startup.log"

# Simple echo to confirm script start - BEFORE complex redirection
echo "Startup script started at $(date)" >> "$LOG"

# Ensure /usr/local/bin is in the PATH (redundant with env but safe)
export PATH=/usr/local/bin:$PATH

# Comment out complex redirection for now
# exec > >(tee -a "$LOG") 2>&1

# Add another simple log entry after PATH export
echo "PATH set, proceeding with script..." >> "$LOG"

echo "🚀 Starting Minikube..." >> "$LOG" # Add explicit logging for commands
minikube start --driver=docker

echo "🔌 Enabling Ingress..." >> "$LOG"
minikube addons enable ingress

echo "🔧 Patching ingress-nginx service to LoadBalancer..." >> "$LOG"
kubectl patch svc ingress-nginx-controller \
  -n ingress-nginx \
  -p '{"spec": {"type": "LoadBalancer"}}'

echo "⏱ Waiting for ingress-nginx-controller to be ready..." >> "$LOG"
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=90s

echo "🌉 Starting Minikube tunnel..." >> "$LOG"
# Note: sudo -S requires password from stdin, which might not work here.
# Relying on NOPASSWD for ccuser set in install_deps.sh
nohup sudo minikube tunnel >> /local/logs/tunnel.log 2>&1 &

echo "🔁 Starting socat port forward from 80 -> 192.168.49.2:80..." >> "$LOG"
nohup sudo socat TCP-LISTEN:80,fork TCP:192.168.49.2:80 >> /local/logs/socat.log 2>&1 &

echo "🐳 Configuring Docker to use Minikube's Docker daemon..." >> "$LOG"
eval $(minikube docker-env)

export TMPDIR=/var/tmp/ccuser-tmp
# Permissions should be okay from install_deps.sh, but double-check if needed
# sudo chown -R ccuser:ccuser /local/
# sudo chmod -R 775 /local/

echo "📦 Installing Keel separately via Helm..." >> "$LOG"
helm repo add keel https://charts.keel.sh
helm repo update
cd /local/repository/helm
helm upgrade --install keel keel/keel -n default -f keel-values.yaml

echo "Waiting for the Keel deployment to become available..." >> "$LOG"
kubectl rollout status deployment/keel -n default

SERVICE_IP=$(kubectl get svc -n default keel -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "Keel is now running and available at http://$SERVICE_IP:9300" >> "$LOG"

echo "Forwarding port 9300 to expose the Keel API..." >> "$LOG"
nohup sudo socat TCP-LISTEN:9300,fork TCP:$SERVICE_IP:9300 >> /local/logs/keel.log 2>&1 &


echo "🔒 Creating Kubernetes secret..." >> "$LOG"

# Secrets are now passed via `env` in the sudo command from init_node.py
if [ -z "$PROD_ENCRYPTION_KEY" ] || [ -z "$PROD_REDIS_PASSWORD" ] || [ -z "$PROD_SESSION_SECRET" ]; then
  echo "Error: Required secret environment variables are not set." >> "$LOG"
  exit 1
fi

kubectl create secret generic campus-connect-config-secrets \
  --from-literal=ENCRYPTION_KEY="$PROD_ENCRYPTION_KEY" \
  --from-literal=REDIS_PASSWORD="$PROD_REDIS_PASSWORD" \
  --from-literal=SESSION_SECRET="$PROD_SESSION_SECRET" \
  --namespace=default \
  --dry-run=client -o yaml | kubectl apply -f -

# Unsetting is less critical now as they were passed via env, not exported globally for long
# unset PROD_ENCRYPTION_KEY PROD_REDIS_PASSWORD PROD_SESSION_SECRET

echo "🚀 Deploying app with Skaffold..." >> "$LOG"
cd /local/repository

echo "current directory: $(pwd)" >> "$LOG"
echo "current user: $(whoami)" >> "$LOG"

skaffold deploy -p prod-deploy -v info 2>&1 | tee -a /local/logs/app.log # Keep tee here for skaffold output

HOSTNAME=$(hostname -f)

echo "" >> "$LOG"
echo "✅ All done! App should be accessible at: http://$HOSTNAME" >> "$LOG"
echo "" >> "$LOG"

# Add final success message
echo "Startup script finished successfully at $(date)" >> "$LOG"
