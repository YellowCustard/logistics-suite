#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 VPS for staging (and later production). Run as root, or with
# sudo from the image's default user (xneelo Cloud and most cloud images log in as `ubuntu`, not root):
#
#   scp deploy/bootstrap.sh ubuntu@<vps>:    (from a checkout; the repository is private)
#   ssh ubuntu@<vps> sudo bash bootstrap.sh
#
# It is safe to re-run: every step checks before it changes anything. The first run stops after printing
# a deploy key to add to GitHub (the repository is private); run it again once the key is added.
set -euo pipefail

REPO_SSH="git@github.com:YellowCustard/logistics-suite.git"
APP_DIR=/opt/logistics
APP_USER=deploy
DUMP_DIR=/var/lib/logistics/dump
LOCAL_REPO_DIR=/var/lib/logistics/restic  # backups when no off-site repository is given

[[ $EUID -eq 0 ]] || { echo "run as root: sudo bash $0"; exit 1; }
# shellcheck disable=SC1091  # provided by the OS
. /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || echo "warning: tested on Ubuntu 24.04 only (found $PRETTY_NAME)"

step() { printf '\n==> %s\n' "$*"; }

step "System packages and automatic security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl git ufw unattended-upgrades fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

step "Docker Engine (official apt repository)"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" >/etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -yq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat >/etc/docker/daemon.json <<'JSON'
{ "log-driver": "json-file", "log-opts": { "max-size": "20m", "max-file": "5" } }
JSON
  systemctl restart docker
fi

step "Deploy user"
if ! id "$APP_USER" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "$APP_USER"
fi
usermod -aG docker "$APP_USER"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"
# Give deploy the keys of whoever is running this: the sudo user's on cloud images, else root's.
KEY_SOURCE=/root/.ssh/authorized_keys
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
  KEY_SOURCE="$(getent passwd "$SUDO_USER" | cut -d: -f6)/.ssh/authorized_keys"
fi
if [[ -s "$KEY_SOURCE" && ! -s /home/$APP_USER/.ssh/authorized_keys ]]; then
  # cloud-init prefixes root's keys with command="echo 'Please login as ...'"; copied as-is, that would lock
  # deploy out. Keep only the key itself from each line.
  { grep -oE '(ssh-(ed25519|rsa|dss)|ecdsa-sha2-nistp[0-9]+|sk-[a-z0-9@.-]+) [A-Za-z0-9+/=]+( .*)?$' "$KEY_SOURCE" || true; } \
    | install -m 600 -o "$APP_USER" -g "$APP_USER" /dev/stdin "/home/$APP_USER/.ssh/authorized_keys"
fi

step "SSH: keys only, no root login (only when a key is installed, so you cannot lock yourself out)"
if [[ -s /home/$APP_USER/.ssh/authorized_keys ]]; then
  cat >/etc/ssh/sshd_config.d/90-logistics.conf <<'SSH'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
SSH
  systemctl reload ssh
else
  echo "skipped: no authorized_keys for $KEY_SOURCE or $APP_USER yet"
fi

step "Firewall: SSH, HTTP, HTTPS only"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

step "Repository deploy key"
KEY="/home/$APP_USER/.ssh/id_ed25519"
if [[ ! -f "$KEY" ]]; then
  sudo -u "$APP_USER" ssh-keygen -q -t ed25519 -N "" -C "logistics-staging@$(hostname)" -f "$KEY"
fi
ssh-keyscan -t ed25519 github.com 2>/dev/null >>"/home/$APP_USER/.ssh/known_hosts"
sort -u -o "/home/$APP_USER/.ssh/known_hosts" "/home/$APP_USER/.ssh/known_hosts"
chown "$APP_USER:$APP_USER" "/home/$APP_USER/.ssh/known_hosts"
if ! sudo -u "$APP_USER" git ls-remote "$REPO_SSH" >/dev/null 2>&1; then
  echo
  echo "Add this key to GitHub: repository Settings > Deploy keys > Add deploy key (leave write access OFF):"
  echo
  cat "$KEY.pub"
  echo
  echo "Then run this script again."
  exit 0
fi

step "Application checkout"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$(dirname "$DUMP_DIR")" "$DUMP_DIR" "$LOCAL_REPO_DIR"
if [[ ! -d "$APP_DIR/.git" ]]; then
  sudo -u "$APP_USER" git clone --quiet "$REPO_SSH" "$APP_DIR"
fi

step "Environment file"
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  read -r -p "Domain for this environment (DNS A record must point here), e.g. staging.example.co.za: " DOMAIN
  read -r -p "Email for Let's Encrypt notices: " ACME_EMAIL
  echo "Off-site restic repository, e.g. s3:https://s3.af-south-1.amazonaws.com/<bucket>/logistics"
  read -r -p "(press Enter to keep backups on this server only): " RESTIC_REPOSITORY
  if [[ -n "$RESTIC_REPOSITORY" ]]; then
    read -r -p "Backup storage access key id: " BACKUP_ACCESS_KEY_ID
    read -r -s -p "Backup storage secret key: " BACKUP_SECRET_ACCESS_KEY; echo
  fi
  rand() { head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c "$1"; }
  fernet=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')
  umask 077
  cat >"$ENV_FILE" <<ENV
DOMAIN=$DOMAIN
ACME_EMAIL=$ACME_EMAIL
POSTGRES_PASSWORD=$(rand 40)
DJANGO_SECRET_KEY=$(rand 60)
FIELD_ENCRYPTION_KEYS=$fernet
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=$DOMAIN
DJANGO_CSRF_TRUSTED_ORIGINS=https://$DOMAIN
DJANGO_SECURE_HSTS_SECONDS=3600
LOG_LEVEL=INFO
BACKUP_DUMP_DIR=$DUMP_DIR
# SSCC labels: replace with your GS1 South Africa company prefix. 0000000 is for testing only.
GS1_COMPANY_PREFIX=0000000
SSCC_EXTENSION_DIGIT=0
ENV
  if [[ -n "$RESTIC_REPOSITORY" ]]; then
    cat >"$APP_DIR/backup.env" <<ENV
RESTIC_REPOSITORY=$RESTIC_REPOSITORY
RESTIC_PASSWORD=$(rand 48)
AWS_ACCESS_KEY_ID=$BACKUP_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=$BACKUP_SECRET_ACCESS_KEY
ENV
  else
    # /data/repo is $LOCAL_REPO_DIR on the host (deploy/compose.staging.yaml). To move off-site later, point
    # RESTIC_REPOSITORY at a bucket, add the AWS_* keys, and run deploy/ops.sh backup.
    cat >"$APP_DIR/backup.env" <<ENV
RESTIC_REPOSITORY=/data/repo
RESTIC_PASSWORD=$(rand 48)
ENV
    echo "warning: backups stay on this server; they will not survive losing the server or the account."
  fi
  chown "$APP_USER:$APP_USER" "$ENV_FILE" "$APP_DIR/backup.env"
  echo
  echo "IMPORTANT: copy $ENV_FILE and $APP_DIR/backup.env to your password manager now. Without"
  echo "FIELD_ENCRYPTION_KEYS and RESTIC_PASSWORD, encrypted personal data and backups cannot be recovered."
fi

step "Scheduled jobs (systemd timers)"
write_unit() {  # name, schedule, command
  cat >"/etc/systemd/system/logistics-$1.service" <<UNIT
[Unit]
Description=logistics $1
Wants=network-online.target
After=docker.service network-online.target

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$3
UNIT
  cat >"/etc/systemd/system/logistics-$1.timer" <<UNIT
[Unit]
Description=logistics $1 schedule

[Timer]
OnCalendar=$2
RandomizedDelaySec=10m
Persistent=true

[Install]
WantedBy=timers.target
UNIT
}
write_unit backup "*-*-* 01:30:00 Africa/Johannesburg" "$APP_DIR/deploy/ops.sh backup"
write_unit maintenance "*-*-* 03:00:00 Africa/Johannesburg" "$APP_DIR/deploy/ops.sh maintenance"
systemctl daemon-reload
systemctl enable --now logistics-backup.timer logistics-maintenance.timer

step "First deploy"
sudo -u "$APP_USER" "$APP_DIR/deploy/ops.sh" deploy

cat <<DONE

Done. Next:
  1. Create the first admin:  sudo -u $APP_USER $APP_DIR/deploy/ops.sh createsuperuser
  2. Run a backup now and a restore drill:
       sudo -u $APP_USER $APP_DIR/deploy/ops.sh backup
       sudo -u $APP_USER $APP_DIR/deploy/ops.sh restore-drill
DONE
