#!/bin/bash
# N4MI Calendar Display — the calendar, full screen in Chromium (Phase 6b).
#
# Runs as the systemd user service calendar-kiosk.service, started when the
# desktop starts. systemd reopens Chromium a few seconds after it closes or
# crashes. Same browser profile and flags as the old desktop icon;
# --password-store=basic stops the desktop keyring prompt that could
# otherwise block the calendar at boot.
#
#   calendar-kiosk stop      close the calendar for maintenance (stays closed)
#   calendar-kiosk start     open it again (a reboot also does this)
#   calendar-kiosk status    is it running?
#   calendar-kiosk run       what the service runs (not for typing by hand)

URL="${CAL_URL:-http://192.168.6.30:8087/display?kiosk=1}"
PROFILE="$HOME/.config/calendar-kiosk"

case "${1:-}" in
  stop)
    systemctl --user stop calendar-kiosk.service && echo "Calendar closed. 'calendar-kiosk start' or a reboot opens it again."
    exit $? ;;
  start)
    systemctl --user start calendar-kiosk.service && echo "Calendar opening."
    exit $? ;;
  status)
    systemctl --user status calendar-kiosk.service --no-pager
    exit $? ;;
  run) ;;
  *)
    echo "Usage: calendar-kiosk stop | start | status" >&2
    exit 2 ;;
esac

BROWSER="$(command -v chromium || command -v chromium-browser)"
if [ -z "$BROWSER" ]; then
  echo "calendar-kiosk: Chromium not found" >&2
  exit 1
fi
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "calendar-kiosk: no desktop session (WAYLAND_DISPLAY not set); it starts with the desktop" >&2
  exit 1
fi

# Chromium marks an unclean shutdown (a power cut) and would offer to restore pages.
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' \
  "$PROFILE/Default/Preferences" 2>/dev/null

exec "$BROWSER" --kiosk --user-data-dir="$PROFILE" --password-store=basic \
  --noerrdialogs --disable-session-crashed-bubble --disable-infobars \
  --disable-features=Translate --check-for-update-interval=31536000 \
  "$URL"
