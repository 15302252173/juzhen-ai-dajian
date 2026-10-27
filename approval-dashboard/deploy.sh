#!/bin/bash
# 首次部署用。不要在已有数据库的目录里用它覆盖 app.py 以外的东西。
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
if [ -z "$INITIAL_ADMIN_PASSWORD" ] || [ -z "$APPROVAL_API_TOKEN" ]; then
  echo "需要 INITIAL_ADMIN_PASSWORD 和 APPROVAL_API_TOKEN 两个环境变量" >&2
  exit 1
fi
if [ ! -f data.db ]; then
  INITIAL_ADMIN_PASSWORD="$INITIAL_ADMIN_PASSWORD" APPROVAL_API_TOKEN="$APPROVAL_API_TOKEN" python3 app.py &
  PID=$!
  sleep 3
  kill $PID || true
  echo "DB created"
else
  echo "DB already exists, not recreating"
fi
cat > /etc/systemd/system/approval-dashboard.service <<EOF
[Unit]
Description=Approval dashboard
After=network.target
[Service]
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 app.py
Restart=always
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now approval-dashboard
systemctl status approval-dashboard --no-pager | head -20
