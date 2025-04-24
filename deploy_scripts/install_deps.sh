#!/bin/bash
#
# Installs necessary dependencies (Docker, Kubernetes tools) and configures
# a non-root user ('ccuser') for deployment operations.
# This script should be run as root or with sudo privileges.
#
set -e

# --- Configuration ---
LOG_DIR="/local/logs"
INSTALL_LOG="$LOG_DIR/install.log"
USERNAME="ccuser"
USER_HOME="/home/$USERNAME"
USER_TMPDIR="/var/tmp/ccuser-tmp"
REPO_DIR="/local/repository"

# --- Logging Setup ---
mkdir -p "$LOG_DIR"
touch "$INSTALL_LOG"
# Ensure root owns the install log initially
chown root:root "$INSTALL_LOG"
chmod 644 "$INSTALL_LOG"
# Redirect all stdout/stderr of this script to the log file
exec > >(tee -a "$INSTALL_LOG") 2>&1

echo "--- Dependency Installation Started: $(date) ---"

# --- Install Core Packages ---
echo "Updating package list and installing core tools (docker, socat, curl)..."
apt-get update
apt-get install -y docker.io socat curl git # Added git

# Verify Docker installation
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker installation failed or 'docker' command not found."
    exit 1
fi
echo "Docker installed: $(docker --version)"

# --- User Setup ---
echo "Configuring user '$USERNAME'..."
if ! id "$USERNAME" &>/dev/null; then
  echo "User '$USERNAME' does not exist, creating..."
  # Create user without password, add to docker and sudo groups
  adduser --disabled-password --gecos "" "$USERNAME"
  usermod -aG docker "$USERNAME"
  usermod -aG sudo "$USERNAME"
  # Grant passwordless sudo privileges to the user
  echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$USERNAME"
  chmod 0440 "/etc/sudoers.d/$USERNAME"
  echo "User '$USERNAME' created and added to docker/sudo groups."

  # Add /usr/local/bin to user's PATH for login shells
  echo 'export PATH=/usr/local/bin:$PATH' >> "$USER_HOME/.profile"
  # Add for non-login shells too (like simple ssh commands)
  echo 'export PATH=/usr/local/bin:$PATH' >> "$USER_HOME/.bashrc"
  chown "$USERNAME:$USERNAME" "$USER_HOME/.profile" "$USER_HOME/.bashrc"
  echo "Added /usr/local/bin to $USERNAME's PATH in .profile and .bashrc."
else
  echo "User '$USERNAME' already exists."
  # Ensure user is in docker group if they already exist
  if ! groups "$USERNAME" | grep -q '\bdocker\b'; then
    echo "Adding existing user '$USERNAME' to docker group."
    usermod -aG docker "$USERNAME"
  fi
   if ! groups "$USERNAME" | grep -q '\bsudo\b'; then
    echo "Adding existing user '$USERNAME' to sudo group."
    usermod -aG sudo "$USERNAME"
  fi
fi

# Note: Setting a password might be a security risk. Evaluate if needed.
# echo "Setting temporary password for $USERNAME (consider removing if not essential)"
# echo "$USERNAME:password" | chpasswd

# --- Directory Setup ---
echo "Configuring directories and permissions..."

# Temporary directory for the user
mkdir -p "$USER_TMPDIR"
chown "$USERNAME:$USERNAME" "$USER_TMPDIR"
chmod 1777 "$USER_TMPDIR" # Sticky bit for shared tmp dir
echo "User temporary directory configured: $USER_TMPDIR"

# Repository directory
mkdir -p "$REPO_DIR"
chown "$USERNAME:$USERNAME" "$REPO_DIR"
chmod 775 "$REPO_DIR" # Allow user/group full access
echo "Repository directory configured: $REPO_DIR"

# Log directory permissions for the user
# Ensure the main log directory allows user access
chown "$USERNAME:$USERNAME" "$LOG_DIR" # User owns the main log dir
chmod 775 "$LOG_DIR"
# Create startup log file owned by the user
touch "$LOG_DIR/startup.log"
chown "$USERNAME:$USERNAME" "$LOG_DIR/startup.log"
chmod 664 "$LOG_DIR/startup.log" # User/group read/write
echo "Log directory permissions updated for user '$USERNAME'."

# TLS directory setup
mkdir -p "/local/tls"
chown "$USERNAME:$USERNAME" "/local/tls"
chmod 775 "/local/tls" # User/group read/write/execute
echo "TLS temporary directory configured: /local/tls"

# --- Install Kubernetes Tools ---

# Install Minikube
echo "Installing Minikube..."
MINIKUBE_URL="https://github.com/kubernetes/minikube/releases/latest/download/minikube-linux-amd64"
curl -Lo minikube-linux-amd64 "$MINIKUBE_URL"
install minikube-linux-amd64 /usr/local/bin/minikube && rm minikube-linux-amd64
# Verify Minikube
if ! command -v minikube &> /dev/null || ! [ -x "$(command -v minikube)" ]; then
    echo "ERROR: Minikube installation failed or command not executable!"
    exit 1
fi
echo "Minikube installed: $(minikube version --short)"

# Install Skaffold
echo "Installing Skaffold..."
SKAFFOLD_URL="https://storage.googleapis.com/skaffold/releases/latest/skaffold-linux-amd64"
curl -Lo skaffold "$SKAFFOLD_URL"
install skaffold /usr/local/bin/ && rm skaffold
# Verify Skaffold
if ! command -v skaffold &> /dev/null || ! [ -x "$(command -v skaffold)" ]; then
    echo "ERROR: Skaffold installation failed or command not executable!"
    exit 1
fi
echo "Skaffold installed: $(skaffold version)"

# Install kubectl
echo "Installing kubectl..."
KUBECTL_STABLE=$(curl -sL https://dl.k8s.io/release/stable.txt)
KUBECTL_URL="https://dl.k8s.io/release/$KUBECTL_STABLE/bin/linux/amd64/kubectl"
curl -LO "$KUBECTL_URL"
install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl
# Verify kubectl
if ! command -v kubectl &> /dev/null || ! [ -x "$(command -v kubectl)" ]; then
    echo "ERROR: kubectl installation failed or command not executable!"
    exit 1
fi
echo "kubectl installed: $(kubectl version --client --short)"

# Install Helm
echo "Installing Helm..."
HELM_SCRIPT_URL="https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3"
curl -fsSL -o get_helm.sh "$HELM_SCRIPT_URL"
chmod 700 get_helm.sh
./get_helm.sh
rm get_helm.sh
# Verify Helm
if ! command -v helm &> /dev/null || ! [ -x "$(command -v helm)" ]; then
    echo "ERROR: Helm installation failed or command not executable!"
    exit 1
fi
echo "Helm installed: $(helm version --short)"

echo "--- Dependency Installation Complete: $(date) ---"
echo "User '$USERNAME' is configured and required tools are installed."
