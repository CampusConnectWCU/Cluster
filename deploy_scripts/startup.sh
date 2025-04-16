#!/bin/bash
set

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

echo "Waiting for the Keel deployment to become available..."
kubectl rollout status deployment/keel -n default

SERVICE_IP=$(kubectl get svc -n default keel -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "Keel is now running and available at http://$SERVICE_IP:9300"

echo "Forwarding port 9300 to expose the Keel API..."
# Rely on NOPASSWD for ccuser
nohup sudo socat TCP-LISTEN:9300,fork TCP:$SERVICE_IP:9300 > /local/logs/keel.log 2>&1 &

echo "🚀 Deploying app with Skaffold..."
cd /local/repository

echo "current directory: $(pwd)"
echo "current user: $(whoami)"

skaffold deploy -p prod-deploy -v info # Output already redirected by exec

HOSTNAME=$(hostname -f)

echo ""
echo "✅ All done! App should be accessible at: http://$HOSTNAME"
echo ""

# Add final success message
echo "Startup script finished successfully at $(date)"
