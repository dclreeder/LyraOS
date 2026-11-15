# LyraOS

One tree for the whole Lyra system (consolidated 2026-11-15, Daniel's design).
Services stay separate processes; they ship, install, and back up as one system.

- core/        lyra-core: free router, tools, SOUL.md, alarm, /tool /announce /call/turn (:8099)
- web/         lyra-web: TV viewer + call page (:8095), kiosk + route-apply
- dashboard/   home dashboard (:8098)
- modules/     shared engines, single-instance rule: tts (Kokoro :8091),
               whisper (:8093), llm (llama.cpp :8094), wakeword (models +
               training + clip pool), wyoming (Wyoming bridges: wake/stt/tts)
- firmware/    sat/ -- speaker-box firmware build tree (build only, never
               flash without Daniel's explicit go-ahead)
- services/    systemd unit files (user services) + drop-ins
- config/      templates only -- real secrets stay OUTSIDE the tree:
               ~/Documents/APIKeys/.env, core/alarm_secrets.json
- data/        big assets, backed up separately: models/ (GGUFs),
               assets/ (alarm tone), wake pool lives in modules/wakeword/data
- memory/      RESERVED: local Lyra's future memory layer (later phase)

External, deliberately not in the tree: ~/Documents/muse-gadget-sdk (vendor),
~/Documents/ReSpeakerFW (vendor), ~/Documents/esphome-probe (the venv the
core runs on), ~/Documents/modules/second-brain (Daniel's call: left out),
~/Documents/APIKeys (secrets).

Compat symlinks remain at the old ~/Documents locations until a clean
reboot cycle proves nothing needs them.
