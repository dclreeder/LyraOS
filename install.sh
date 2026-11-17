#!/bin/bash
# LyraOS install: place the user units, reload, enable the roster.
# Does not start anything that is not already running discipline:
# enable + start is safe to re-run.
set -e
cd "$(dirname "$0")"
U=~/.config/systemd/user
mkdir -p "$U"
cp services/*.service services/*.path "$U/"
for d in services/*.service.d; do [ -d "$d" ] && mkdir -p "$U/$(basename "$d")" && cp "$d"/*.conf "$U/$(basename "$d")/"; done
export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
systemctl --user daemon-reload
systemctl --user enable lyra-core lyra-dashboard lyra-kiosk lyra-kokoro-tts lyra-web lyra-whisper lyra-wyoming-stt lyra-wyoming-tts lyra-wyoming-wake lyra-route-apply lyra-route-watch.path
loginctl enable-linger "$USER" || true
echo 'LyraOS units installed and enabled.'
