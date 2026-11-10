#!/bin/bash
# N4MI Calendar Display — install or update the shelf Pi's helper and kiosk (Phase 6b).
#
# Run on the Pi, as the desktop login (not root), from a clone of the repository:
#   cd ~/n4mi-calendar-display && git pull && bash display/install.sh
# Then reboot. Safe to run again: it replaces its own files and nothing else.
#
# What it does:
#   ~/.local/bin/calendar-agent      the helper (health, night screen, restart)
#   ~/.local/bin/calendar-kiosk      the calendar full screen (stop / start / status)
#   ~/.config/systemd/user/calendar-agent.service and calendar-kiosk.service
#   ~/.config/autostart/n4mi-calendar.desktop   starts both when the desktop starts
#   /etc/polkit-1/rules.d/50-n4mi-calendar.rules   lets this login restart or
#        turn off the Pi from the health panel (asks for your password once, via sudo)
#
# With the read-only overlay on (Phase 6c), turn it off first; changes made
# while it is on are lost at the next reboot.

set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal login, not with sudo." >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
UNIT_DIR="$HOME/.config/systemd/user"
AUTO="$HOME/.config/autostart"

if grep -qs ' / overlay ' /proc/mounts; then
  echo "The read-only overlay is on. Turn it off (sudo raspi-config), reboot, then run this again." >&2
  exit 1
fi

mkdir -p "$BIN" "$UNIT_DIR" "$AUTO"
install -m 755 "$HERE/calendar-agent.py" "$BIN/calendar-agent"
install -m 755 "$HERE/calendar-kiosk.sh" "$BIN/calendar-kiosk"
install -m 644 "$HERE/calendar-agent.service" "$UNIT_DIR/calendar-agent.service"
install -m 644 "$HERE/calendar-kiosk.service" "$UNIT_DIR/calendar-kiosk.service"
echo "Installed the helper and the kiosk script to $BIN"

# Autostart. Raspberry Pi OS's labwc session runs ~/.config/autostart entries
# (lxsession-xdg-autostart), which keeps the normal desktop untouched.
if ! grep -qs "xdg-autostart" /etc/xdg/labwc/autostart; then
  echo "Warning: /etc/xdg/labwc/autostart doesn't run XDG autostart entries on this system." >&2
  echo "         The entries below may not start at login; see docs/pi-setup.md." >&2
fi
# One entry: hand the desktop session's settings to systemd, then start both services.
rm -f "$AUTO/calendar-kiosk.desktop" "$AUTO/calendar-agent.desktop"   # from the first 6b version
cat > "$AUTO/n4mi-calendar.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=N4MI Calendar
Comment=Starts the calendar (full screen) and its helper inside this desktop session
Exec=sh -c "systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR XDG_SESSION_TYPE XDG_CURRENT_DESKTOP DISPLAY; systemctl --user restart calendar-agent.service calendar-kiosk.service"
X-GNOME-Autostart-enabled=true
EOF
echo "Added the autostart entry in $AUTO"

# Restart / Shut down from the health panel.
RULE=/etc/polkit-1/rules.d/50-n4mi-calendar.rules
TMP="$(mktemp)"
cat > "$TMP" <<EOF
// N4MI Calendar Display: let the desktop login restart or turn off the Pi
// (Restart / Shut down in the shelf's health panel). Installed by display/install.sh.
polkit.addRule(function (action, subject) {
  var ids = ["org.freedesktop.login1.reboot", "org.freedesktop.login1.reboot-multiple-sessions",
             "org.freedesktop.login1.power-off", "org.freedesktop.login1.power-off-multiple-sessions"];
  if (subject.user == "$USER" && ids.indexOf(action.id) >= 0) {
    return polkit.Result.YES;
  }
});
EOF
if ! cmp -s "$TMP" "$RULE" 2>/dev/null; then
  echo "Installing the Restart / Shut down permission (sudo may ask for your password)..."
  sudo install -m 644 "$TMP" "$RULE"
fi
rm -f "$TMP"

systemctl --user daemon-reload
echo
echo "Done. Reboot to start everything:  sudo reboot"
echo "Check after the reboot: systemctl --user status calendar-agent calendar-kiosk --no-pager"
