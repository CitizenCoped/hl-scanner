#!/usr/bin/env bash
# bin/do_bootstrap.sh — run ONCE on the EC2 box after SSH'ing in. Idempotent.
set -euo pipefail
if [[ "$(id -u)" -ne 0 ]]; then exec sudo -E "$0" "$@"; fi

REGION="ap-northeast-1"
APP=/opt/scanner
DATA=$APP/data
SOCKET_DIR=/run/valkey
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

bold "=== HL Scanner bootstrap ==="

# 1. System deps
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3.12 python3.12-venv python3-pip \
  build-essential git tmux jq curl ca-certificates \
  postgresql-client-16 valkey-server xfsprogs unzip

# uv — install then copy the binary to a world-readable location so the
# unprivileged `scanner` user can execute it (a symlink into /root is not
# traversable by other users).
if [[ ! -x /usr/local/bin/uv ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  install -m 755 /root/.local/bin/uv /usr/local/bin/uv
fi

# s5cmd — resolve the correct arm64 asset from the GitHub API; non-fatal so a
# transient download hiccup never blocks the rest of bootstrap.
if ! command -v s5cmd >/dev/null; then
  S5_URL=$(curl -fsSL https://api.github.com/repos/peak/s5cmd/releases/latest \
    | jq -r '.assets[].browser_download_url' \
    | grep -Ei 'Linux.*(arm64).*\.tar\.gz$' | head -n1 || true)
  if [[ -n "$S5_URL" ]]; then
    curl -fsSL "$S5_URL" | tar xz -C /usr/local/bin s5cmd || true
  fi
fi

# CloudWatch agent
if ! command -v amazon-cloudwatch-agent-ctl >/dev/null; then
  curl -L "https://amazoncloudwatch-agent-${REGION}.s3.${REGION}.amazonaws.com/ubuntu/arm64/latest/amazon-cloudwatch-agent.deb" -o /tmp/cwagent.deb
  dpkg -i /tmp/cwagent.deb || true
fi

# 2. User + dirs
id -u scanner &>/dev/null || useradd -r -m -d $APP -s /usr/sbin/nologin scanner
install -d -o scanner -g scanner $APP $DATA $APP/logs

# 3. Mount the 200 GB data EBS volume at /opt/scanner/data
DEV=$(lsblk -dno NAME,SIZE | awk '$2=="200G"{print $1; exit}')
if [[ -n "$DEV" ]]; then
  blkid "/dev/$DEV" >/dev/null 2>&1 || mkfs.xfs -L scanner-data "/dev/$DEV"
  UUID=$(blkid -s UUID -o value "/dev/$DEV")
  grep -q "$UUID" /etc/fstab || echo "UUID=$UUID $DATA xfs defaults,noatime 0 2" >> /etc/fstab
  mountpoint -q $DATA || mount $DATA
  chown scanner:scanner $DATA
fi

# 4. Valkey sidecar (Unix socket)
# The valkey-server deb ships and auto-enables its own valkey-server.service.
# That stock unit runs as user `valkey` (which cannot read our root:scanner
# valkey.conf) so it crash-loops; worse, it declares RuntimeDirectory=valkey,
# so each failed start/stop deletes the shared /run/valkey out from under our
# scanner-valkey sidecar, destroying the socket. Mask it so only our unit owns
# /run/valkey.
systemctl disable --now valkey-server.service 2>/dev/null || true
systemctl mask valkey-server.service
install -d -o scanner -g scanner $SOCKET_DIR $DATA/valkey
cat > /etc/valkey/valkey.conf <<EOF
bind 127.0.0.1 -::1
port 0
unixsocket $SOCKET_DIR/valkey.sock
unixsocketperm 770
dir $DATA/valkey
maxmemory 4gb
maxmemory-policy allkeys-lru
save ""
appendonly no
EOF

# The valkey deb ships /etc/valkey owned valkey:valkey mode 750, which the
# scanner user cannot read. Grant read access via group ownership.
chown root:scanner /etc/valkey /etc/valkey/valkey.conf
chmod 750 /etc/valkey
chmod 640 /etc/valkey/valkey.conf

# 5. systemd units
cat > /etc/systemd/system/scanner-valkey.service <<'EOF'
[Unit]
Description=Scanner Valkey sidecar
After=network.target
[Service]
Type=simple
User=scanner
Group=scanner
RuntimeDirectory=valkey
RuntimeDirectoryMode=0770
ExecStart=/usr/bin/valkey-server /etc/valkey/valkey.conf
Restart=always
RestartSec=2s
[Install]
WantedBy=multi-user.target
EOF

# SE-approved deviation from BUILD_GUIDE §18: these units log to files under
# $APP/logs instead of journald so the released CloudWatch agent's "files"
# collector can ship them (the agent's journald collector is unreleased).
for svc in ingestor feature-worker alerter markouts; do
  case "$svc" in
    ingestor) module="ws_client" ;;
    feature-worker) module="bar_builder" ;;
    *) module="${svc//-/_}" ;;
  esac
  cat > /etc/systemd/system/scanner-$svc.service <<EOF
[Unit]
Description=Scanner $svc
Requires=scanner-valkey.service
After=scanner-valkey.service network-online.target
[Service]
Type=simple
User=scanner
Group=scanner
WorkingDirectory=$APP/app
EnvironmentFile=$APP/.env.production
ExecStart=$APP/app/.venv/bin/python -u -m scanner.$module
Restart=always
RestartSec=5s
StartLimitBurst=10
StartLimitIntervalSec=60
StandardOutput=append:$APP/logs/$svc.log
StandardError=append:$APP/logs/$svc.log
[Install]
WantedBy=multi-user.target
EOF
done

cat > /etc/systemd/system/scanner-archive.service <<EOF
[Unit]
Description=Scanner nightly S3 archive
[Service]
Type=oneshot
User=scanner
Group=scanner
WorkingDirectory=$APP/app
EnvironmentFile=$APP/.env.production
ExecStart=$APP/app/.venv/bin/python -u -m scanner.archive_to_s3
EOF

cat > /etc/systemd/system/scanner-archive.timer <<'EOF'
[Unit]
Description=Run scanner archive nightly
[Timer]
OnCalendar=*-*-* 02:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
EOF

# Stats exporter: publishes stats.json to the dashboard S3 bucket every minute.
cat > /etc/systemd/system/scanner-stats.service <<EOF
[Unit]
Description=Scanner stats exporter (publishes stats.json to S3)
After=network-online.target
[Service]
Type=oneshot
User=scanner
Group=scanner
WorkingDirectory=$APP/app
EnvironmentFile=$APP/.env.production
ExecStart=$APP/app/.venv/bin/python -u -m scanner.stats_exporter
EOF

cat > /etc/systemd/system/scanner-stats.timer <<'EOF'
[Unit]
Description=Run scanner stats exporter every minute
[Timer]
OnCalendar=*:0/1
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now scanner-valkey.service
systemctl enable scanner-archive.timer
systemctl enable --now scanner-stats.timer

# 6. Force STS regional endpoint for any boto3 call from this host
install -d -o scanner -g scanner $APP/.aws
cat > $APP/.aws/config <<EOF
[default]
region = $REGION
sts_regional_endpoints = regional
EOF
chown -R scanner:scanner $APP/.aws

# 7. CloudWatch agent — ship the four service log files at Infrequent-Access
# class, 7-day retention. (The released agent's log collector reads files; the
# services write to $APP/logs/*.log via systemd StandardOutput=append.)
# SE-approved deviation from BUILD_GUIDE §18: uses the "files" collector instead
# of "journald", which the released CloudWatch agent does not yet support.
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<EOF
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {"file_path":"$APP/logs/ingestor.log",       "log_group_name":"/scanner/ingestor",       "log_stream_name":"{instance_id}","log_group_class":"INFREQUENT_ACCESS","retention_in_days":7},
          {"file_path":"$APP/logs/feature-worker.log", "log_group_name":"/scanner/feature-worker", "log_stream_name":"{instance_id}","log_group_class":"INFREQUENT_ACCESS","retention_in_days":7},
          {"file_path":"$APP/logs/alerter.log",        "log_group_name":"/scanner/alerter",        "log_stream_name":"{instance_id}","log_group_class":"INFREQUENT_ACCESS","retention_in_days":7},
          {"file_path":"$APP/logs/markouts.log",       "log_group_name":"/scanner/markouts",       "log_stream_name":"{instance_id}","log_group_class":"INFREQUENT_ACCESS","retention_in_days":7}
        ]
      }
    }
  }
}
EOF
amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json -s || true

echo
bold "✓ Bootstrap complete."
echo "Next: clone the app to $APP/app, run 'uv sync' as the scanner user,"
echo "then 'systemctl enable --now scanner-{ingestor,feature-worker,alerter,markouts}'."
