# LyraOS

One tree for the whole Lyra system (consolidated 2026-11-15, Daniel's design).
Services stay separate processes; they ship, install, and back up as one system.

- core/        lyra-core: free router (brain: local Nemotron Nano 9B v2
               via lyra-llm, :8094), tools, SOUL.md, alarm,
               /tool /announce /rtc/offer (:8099)
- web/         lyra-web: TV viewer (:8095), kiosk + route-apply
- dashboard/   home dashboard (:8098)
- modules/     shared engines, single-instance rule: tts (Kokoro :8091),
               wakeword (models + training + clip pool), wyoming
               (Wyoming bridges: wake/stt/tts). STT is split: Whisper
               large-v3-turbo (whisper.cpp, :8093) transcribes the
               WakeGate path; Parakeet (:8096, external by design in
               ~/Documents/parakeet.cpp) serves RTC only.
- services/    systemd unit files (user services) + drop-ins
- config/      templates only -- real secrets stay OUTSIDE the tree:
               ~/Documents/APIKeys/.env, core/alarm_secrets.json
- data/        big assets, backed up separately: assets/ (alarm tone),
               wake pool lives in modules/wakeword/data
- memory/      Lyra's memory layer: the record, the claim store,
               entity pages, sources, curation reports (memory/PLAN.md)

External, deliberately not in the tree: ~/Documents/muse-gadget-sdk (vendor),
~/Documents/ReSpeakerFW (vendor), ~/Documents/modules/second-brain
(Daniel's call: left out), ~/Documents/APIKeys (secrets). The core's
Python venv lives in the tree at core/venv (gitignored, like all venvs here).

Compat symlinks remain at the old ~/Documents locations until a clean
reboot cycle proves nothing needs them.
