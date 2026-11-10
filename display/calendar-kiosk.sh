#!/bin/bash
# N4MI Calendar Display — keeps the calendar open full screen (Phase 6b).
#
# Started by the desktop's autostart at login. If Chromium closes or crashes,
# it is opened again after a few seconds. Uses the same browser profile and
# flags as the old desktop icon; --password-store=basic stops the desktop
# keyring prompt that could otherwise block the calendar at boot.
#
#   calendar-kiosk          run (normally from autostart)
#   calendar-kiosk stop     close the calendar for maintenance and stop it reopening
#   calendar-kiosk start    open it again (a reboot also does this)
#
# The "stop" flag lives in /run/user/<id>, which is cleared at every reboot.

URL="${CAL_URL:-http://192.168.6.30:8087/display?kiosk=1}"
PROFILE="$HOME/.config/calendar-kiosk"
FLAG="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/calendar-kiosk.stop"
BROWSER="$(command -v chromium || command -v chromium-browser)"

case "$1" in
  stop)
    touch "$FLAG"
    pkill -f -- "--user-data-dir=$PROFILE" && echo "Calendar closed. Run 'calendar-kiosk start' or reboot to open it again." \
      || echo "Calendar wasn't running; it won't reopen until 'calendar-kiosk start' or a reboot."
    exit 0 ;;
  start)
    rm -f "$FLAG"
    if pgrep -f -- "--user-data-dir=$PROFILE" >/dev/null; then echo "Already open."; exit 0; fi
    export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}" XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    nohup "$0" >/dev/null 2>&1 &
    echo "Calendar opening."
    exit 0 ;;
esac

if [ -z "$BROWSER" ]; then
  echo "calendar-kiosk: Chromium not found" >&2
  exit 1
fi

# Chromium marks an unclean shutdown (a power cut) and would offer to restore pages.
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' \
  "$PROFILE/Default/Preferences" 2>/dev/null

while [ ! -f "$FLAG" ]; do
  "$BROWSER" --kiosk --user-data-dir="$PROFILE" --password-store=basic \
    --noerrdialogs --disable-session-crashed-bubble --disable-infobars \
    --disable-features=Translate --check-for-update-interval=31536000 \
    "$URL" >/dev/null 2>&1
  [ -f "$FLAG" ] && break
  sleep 3
done
