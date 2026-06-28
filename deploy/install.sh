#!/bin/bash
# Linux (systemd) installer for the GDELT ingest pipeline.
#
# NOTE: Production runs on Windows (rainbow-boi) via Task Scheduler — see
# deploy/register_task.ps1 and deploy/register_dashboard.ps1, which are the
# authoritative deploy. This script is the Linux-parity reference installer:
# it sets up the venv, the 15-minute ingest service, and its timer.
set -euo pipefail

# Resolve repo dir from this script's location (portable, no hardcoded paths).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
PY="$VENV_DIR/bin/python"

echo "=== GDELT Pipeline Install (Linux) ==="
echo "Repo: $REPO_DIR"

# Create venv and install pinned deps.
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

# Create data directories.
mkdir -p "$REPO_DIR/data/raw" "$REPO_DIR/data/logs"

# Generate + install the systemd ingest service (paths resolved for this host)
# and copy the timer.
echo "Installing systemd units..."
sudo tee /etc/systemd/system/gdelt-ingest.service >/dev/null <<EOF
[Unit]
Description=GDELT v2 data ingest (15-minute pull)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
ExecStart=$PY $REPO_DIR/gdelt_ingest.py
Nice=10
EOF
sudo cp "$REPO_DIR/deploy/gdelt-ingest.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable gdelt-ingest.timer
sudo systemctl start gdelt-ingest.timer

echo ""
echo "Timer status:"
systemctl list-timers gdelt-ingest.timer --no-pager
echo ""
echo "=== Done ==="
echo "Next steps:"
echo "  1. Run backfill:  cd $REPO_DIR && .venv/bin/python gdelt_backfill.py --days 60"
echo "  2. Check status:  cd $REPO_DIR && .venv/bin/python gdelt_status.py"
echo "  3. Monitor logs:  journalctl -u gdelt-ingest -f"
