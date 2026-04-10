#!/bin/bash
set -euo pipefail

REPO_DIR="$HOME/projects/analytics-projects/gdelt-events"
VENV_DIR="$REPO_DIR/.venv"

echo "=== GDELT Pipeline Install ==="

# Create venv and install deps
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q requests duckdb

# Create data directories
mkdir -p "$REPO_DIR/data/raw" "$REPO_DIR/data/logs"

# Install systemd units
echo "Installing systemd units..."
sudo cp "$REPO_DIR/deploy/gdelt-ingest.service" /etc/systemd/system/
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
