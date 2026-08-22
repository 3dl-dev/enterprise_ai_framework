#!/usr/bin/env bash
# Install/refresh the funnel-watchdog on the gateway VM. Idempotent.
# Run ON the gateway (or via: ssh baron@gateway 'sudo bash -s' < install.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 0755 "$HERE/funnel-watchdog"          /usr/local/bin/funnel-watchdog
install -m 0644 "$HERE/funnel-watchdog.service"  /etc/systemd/system/funnel-watchdog.service
install -m 0644 "$HERE/funnel-watchdog.timer"    /etc/systemd/system/funnel-watchdog.timer

systemctl daemon-reload
systemctl enable --now funnel-watchdog.timer
systemctl restart funnel-watchdog.timer

echo "installed. timer status:"
systemctl status funnel-watchdog.timer --no-pager | tail -4
