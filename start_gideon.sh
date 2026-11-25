#!/usr/bin/env bash
# Gideon Launcher
# Starts and manages Gideon as a resilient background systemd service.

set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure Wayland and Hyprland environment variables are imported to systemd user manager
systemctl --user import-environment HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY DISPLAY XDG_CURRENT_DESKTOP XDG_RUNTIME_DIR 2>/dev/null || true

if [[ "${1:-}" == "--restart" || "${1:-}" == "-r" ]]; then
    echo "Restarting Gideon daemon and 3D Quickshell Orb..."
    systemctl --user daemon-reload
    systemctl --user restart gideon.service gideon-ui.service
    echo "Gideon restarted successfully."
    exit 0
fi

if [[ "${1:-}" == "--stop" || "${1:-}" == "-s" ]]; then
    echo "Stopping Gideon services..."
    systemctl --user stop gideon.service gideon-ui.service
    echo "Gideon stopped."
    exit 0
fi

if [[ "${1:-}" == "--status" ]]; then
    systemctl --user status gideon.service gideon-ui.service --no-pager
    exit 0
fi

if [[ "${1:-}" == "--logs" || "${1:-}" == "-l" ]]; then
    journalctl --user -u gideon.service -u gideon-ui.service -f
    exit 0
fi

# Ensure systemd user units are reloaded and started
systemctl --user daemon-reload 2>/dev/null || true

if ! systemctl --user is-active --quiet gideon-ui.service; then
    echo "Starting GIDEON 3D Quickshell Orb overlay..."
    systemctl --user start gideon-ui.service
fi

if ! systemctl --user is-active --quiet gideon.service; then
    echo "Starting GIDEON executive daemon..."
    systemctl --user start gideon.service
fi

# If invoked interactively in a terminal without args, stream logs for convenience
if [ -t 1 ] && [ -t 0 ]; then
    echo "Gideon is running in background (systemd). Showing live logs (Ctrl+C to detach):"
    journalctl --user -u gideon.service -f
fi
