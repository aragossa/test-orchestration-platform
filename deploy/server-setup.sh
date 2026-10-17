#!/usr/bin/env bash
# One-time bootstrap for a fresh Ubuntu 24.04 VPS. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/deploy/server-setup.sh | bash
# Then put docker-compose.prod.yml, Caddyfile and .env into /opt/orchestrator.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl ufw

# Docker Engine + compose plugin from Docker's repository.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

# Firewall: SSH + HTTP/HTTPS only. Postgres is never exposed.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

# Unprivileged deploy user that can talk to Docker.
id -u deploy >/dev/null 2>&1 || useradd -m -s /bin/bash deploy
usermod -aG docker deploy
mkdir -p /opt/orchestrator
chown deploy:deploy /opt/orchestrator
if [ -d /root/.ssh ] && [ -f /root/.ssh/authorized_keys ]; then
  install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
  install -m 600 -o deploy -g deploy /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
fi

echo "Done. Next: copy deploy/docker-compose.prod.yml, deploy/Caddyfile and a filled .env to /opt/orchestrator"
echo "and run: docker compose -f docker-compose.prod.yml up -d"
