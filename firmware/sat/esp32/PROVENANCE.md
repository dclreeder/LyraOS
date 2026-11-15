# Provenance — firmware/esp32 tree

Copied 2026-10-30 from `~/Documents/muse-gadget-sdk-probe/esp32`
(upstream muse-gadget-sdk @ 1b894f5, working tree clean except the
untracked probe overlay) as the source baseline for the Lyra Sat Gadget
board port spike. The source trees were not modified.

Excluded from the copy: `build-probe-175c-rtv/` (probe build output),
`dev_signing_key.pem` (signing key -- never copied into a product tree),
`devices/sdkconfig.probe-rtv` (probe-only overlay), `__pycache__`.
`managed_components/` IS copied: the vendored dependencies make this tree
build offline and keep it reproducible.

Sat Gadget changes vs upstream (all other files pristine):
- ADDED devices/sdkconfig.lyra-satgadget, devices/lyra-satgadget.md,
  partitions_satgadget.csv, main/voice_board_lyra_flex.c, PROVENANCE.md
- PATCHED main/Kconfig.projbuild (HOMEHUB_LED_BACKEND_LYRA_FLEX choice),
  main/CMakeLists.txt (voice board + led_strip selection),
  main/led_status.c (FLEX shares the Lite WS2812 path),
  tools/board.sh (lyra-satgadget profile), version.txt (2026.10.30-sat0);
  second Kconfig patch found during the spike: HOMEHUB_VOICE's
  `depends on` now also accepts HOMEHUB_LED_BACKEND_LYRA_FLEX (without it
  Kconfig silently drops the whole voice stack for this board).
- Every patch keeps a sibling backup: `<file>.pre-sat0`.

Signing: sdkconfig.defaults requires signed app binaries
(SECURE_SIGNED_APPS_NO_SECURE_BOOT). A FRESH dev signing key was generated
in this tree (`dev_signing_key.pem`, `idf.py secure-generate-signing-key`,
2026-10-30) -- it is this project's own key, not copied from any other
tree. Like every gadget tree: never distribute a .bin signed with it.

BUILD PATH GOTCHA (found 2026-10-30, cost an hour): ESP-IDF v6's
esp_phy phy_init_data step fails from a project path containing TWO spaces
("Lyra Sat Gadget") -- its @toolchain/cflags response-file path is mangled
through the ninja/sh layer and the picolibc specs never reach that one
compile (every other TU is unaffected; the same compile succeeds when run
by hand). The one-space "Lyra Gadget" fork path builds fine. Workaround
used for sat0, and the standing recipe for this project:

  rsync -a --delete --exclude build-lyra-satgadget \
    "~/Documents/Lyra Sat Gadget/firmware/esp32/" ~/lyra-sat-build/esp32/
  cd ~/lyra-sat-build/esp32 && tools/board.sh lyra-satgadget build
  rsync -a ~/lyra-sat-build/esp32/build-lyra-satgadget/ \
    "~/Documents/Lyra Sat Gadget/firmware/esp32/build-lyra-satgadget/"

Also: after editing Kconfig files, `idf.py build` alone may skip the
reconfigure (stale sdkconfig, 5-step no-op build). Run the board.sh
differences through `idf.py ... reconfigure` once, or delete the build
dir's sdkconfig, and always read the GENERATED sdkconfig afterwards.

## sat1 changes (2026-10-30, local path + OTA removal)

- ADDED main/sat_local.c + main/sat_local.h: the local-path TCP client
  (transport doc) -- archived framing to the Fedora lyra-satgadget
  service, CTRL dispatcher incl. start_call -> voice_request_call().
  Adapted from the archived satellite firmware's sat_client.c/audio.c.
- ADDED main/sat_proto.h: copied verbatim from the archived satellite
  firmware (the wire contract).
- PATCHED main/voice.c + main/voice.h (.pre-sat1): voice_request_call()
  / voice_call_active() / voice_ready() exports; run_call() sets the
  call-active flag; the voice task consumes network call requests.
- PATCHED main/app.c (.pre-sat1): sat_local_start() after voice_init().
- PATCHED main/CMakeLists.txt (.pre-sat1): sat_local.c in HOMEHUB_VOICE.
- PATCHED main/Kconfig.projbuild (.pre-sat1): LYRA_SAT_SERVER_HOST /
  LYRA_SAT_SERVER_PORT / LYRA_SAT_DEVICE_ID.
- PATCHED devices/sdkconfig.lyra-satgadget (.pre-sat1): OTA REMOVED at
  Daniel's order (HOMEHUB_OTA_ENABLED off, bootloader rollback off) and
  **Daniel's SDK token now lives in this overlay** as
  CONFIG_GADGET_SDK_TOKEN (injected value-blind from
  /var/lib/musegadget/sdk_token, 2026-10-30). This overlay is a local,
  untracked fragment -- never commit or share it (or any build output
  sdkconfig / .bin) with the token in it.
- REWROTE partitions_satgadget.csv (.pre-sat1): SINGLE-APP -- nvs,
  phy_init, one factory app (0x7C0000), coredump. No otadata.
- version.txt -> 2026.10.30-sat1.

## sat2 changes (2026-10-30, pairing auto-confirm)

- PATCHED main/app.c (.pre-sat2): on the lyra-satgadget board only
  (CONFIG_HOMEHUB_LED_BACKEND_LYRA_FLEX), a new auto_confirm_task is
  spawned at the end of on_pairing_client_finished() -- right after the
  confirmation window is armed. After a 500 ms delay (keeps the
  encrypted "confirm_required" record ahead of "pairing_confirmed" on
  the wire) it runs the exact button-handler sequence:
  link_pairing_confirm_active_session() + notify_pairing_confirmed().
  Why: sat1's app pairing flow stalls at "Confirm by pressing the
  button on your device" and the XIAO BOOT button is inside the closed
  speaker box. The gate is unchanged -- confirmation still requires an
  armed, unexpired session inside the unpaired provisioning flow;
  button.c and the button's other roles (setup window, long-press
  setup reset) are untouched.
- version.txt -> 2026.10.30-sat2.

## sat4 changes (2026-10-30, alarm volume + alarm/timer engine)

One build on top of sat3 (config-only change), version
2026.10.30-sat4. Backups: sat_local.c / CMakeLists.txt / version.txt
as `.pre-sat4`.

- main/alerts.c + alerts.h (new): on-device alarm/timer engine
  modeled on the AMOLED gadget's muse_alarms semantics -- up to 6
  NVS-persisted alarms (config_store JSON doc, key `alarms_v1`;
  once/daily/weekdays; 500 ms tick vs SNTP local time, TZ
  EST5EDT,M3.2.0/2,M11.1.0; snooze +9 min RAM-held; 10-min cap;
  90-s recent-stop record so a snooze after a gate-stop still
  lands) and up to 4 RAM-held timers (never persisted; snooze does
  not apply). Embedded HomePod alarm WAV (main/alarm.wav, sha256
  72fc913611ae331a0bac303f8371e043e8005017e91b8f48d0fee176e4fa89d1,
  byte-identical to the gadget tunnel copy), resampled 16k->48k,
  looped at the alarm volume by sat_local's speaker task; ducking
  to 25% with 20 s / link-drop auto-restore. Tick runs on a PSRAM
  stack and never writes flash (store task owns tick-originated
  NVS writes; CTRL mutations write from the net task).
- main/sat_local.c: CTRL set_alarm_volume -> config key
  `alarm_volume` (default 100), reported as `alarm_volume` in
  hello/status; the alarms_*/timers_*/alert_* CTRL verbs replacing
  the stubs, pushing `alerts_state` after mutations and on engine
  changes (callmon poll); send_ctrl_json cap 512 -> 1280 B (static
  frame buffer); AUDIO_DOWN dropped while an unducked alert owns
  the speaker, mixed over a ducked alert otherwise; SNTP started
  once the station has an IP.
- main/CMakeLists.txt: alerts.c added to the voice sources;
  alarm.wav embedded (EMBED_FILES) for the LYRA_FLEX voice build.
- Build: EXIT=0, zero warnings; generated sdkconfig identical to
  sat3's (full diff); bin 1,839,104 B (+327,680 vs sat3);
  .iram0.text unchanged, .flash.text +14,728, .dram0.bss +5,296.
- Flash verified 4x hash / EXIT=0 on Daniel's go-ahead; alarm
  volume relay + round-trip and the alarms_list state push verified
  live. KNOWN FAULT at landing: the device resets ~1 s after an
  alert fires (timer fired on time, alerts_state showed it, then
  reset) -- alert playback start is the suspect area, power
  brownout at full gain the alternative; full forensics in
  docs/local-path-report-2026-10-30.md (sat4 section).

## sat5 changes (2026-10-30, alert-reset root cause + fix; two flashes)

Version 2026.10.30-sat5. Backups `.pre-sat5` (sat_local.c, alerts.c,
devices/sdkconfig.lyra-satgadget, version.txt).

- Diagnostics first: CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH=y (ELF) in
  the sat board defaults; step logging on the alert path; esp_reset_
  reason boot log + `reset_reason` in the status JSON. The repro's
  coredump (task 'sat_spk', assert
  esp_task_stack_is_sane_cache_disabled, cache_utils.c:126) showed
  the sat4 reset was a PANIC: the PSRAM-stack speaker task called
  alarm_volume_pct() per frame, and that getter re-read NVS every
  call -- any flash API access from a PSRAM-stack task asserts by
  design. NOT a brownout (reset_reason was 4=PANIC; the sat4 50%
  test was void as a power discriminator).
- Fix (main/sat_local.c only): RAM caches for both volume percents.
  volume_pct()/alarm_volume_pct() still read NVS but now run only
  on internal-stack contexts (net task, hello/status, start) and
  refresh the caches; the setters update them; the speaker task's
  alert path uses volume_pct_cached()/alarm_volume_pct_cached(),
  which never touch flash. Audit: no remaining flash access is
  reachable from any PSRAM-stack task in the alert path.
- Build/flash: two clean-ish builds (first a full clean build for
  the sdkconfig change), zero warnings in changed files, sdkconfig
  delta vs sat4 = coredump block + its Kconfig dependents only;
  bin 1,904,640 B (+65,536 vs sat4). Both flashes 4x "Hash of data
  verified" / EXIT=0 on Daniel's approval.
- Verified live (details + the honest test-alert timeline in
  docs/local-path-report-2026-10-30.md sat5 section): sustained
  alert playback 16-21 s at 100% (sat4 died ~1 s), and a clean
  voice GATE-STOP at 50% (gate 0.839 -> alert_stop -> downlink
  resumed). One power-on-class reset at ~21 s of 100% playback
  remains open -- leading suspect is USB bench power sag at max
  gain; NOT isolated (Daniel banned further live alert tests;
  revisit on 12 V). Fedora side gained VAD-triggered alert
  ducking (service-only change, satgadget_service.py.pre-sat5).
- Left in on purpose: coredump-to-flash and the alert diag step
  logging -- headless forensics earned their keep this run.

## sat6 changes (2026-10-31, XVF mic DSP tuning + alarm priority + call greeting; one stacked flash)

Version 2026.10.31-sat6. Backups `.pre-sat6` (voice_board_lyra_flex.c,
voice.c, voice.h, alerts.c, alerts.h, sat_local.c,
devices/sdkconfig.lyra-satgadget, version.txt, PROVENANCE.md). Three
independent changes, each separable:

- XVF3800 mic DSP tuning (main/voice_board_lyra_flex.c only). The XVF
  ran Seeed's fine-tune over factory DSP defaults and nothing here
  ever tuned it; forensics 2026-10-31: in a quiet room the AGC rides
  gain to its 64x ceiling while waiting, a first syllable (even a
  whisper) arrives amplified into the rail (one-sided -32767 pinning),
  then the fast attack collapses the level 18-34 dB and mangles the
  rest of the sentence. New xvf_apply_tuning(), called in
  voice_board_init right after the unmute, speaks the ctl v2 protocol
  (resid/cmd/len framing; reads are cmd|0x80 + status byte, 64 =
  retry) per Seeed's xvf_host.py map (~/Documents/ReSpeakerFW) and
  the XMOS tuning guide (XM-014888-PC). It logs before/after for
  every value and applies, on the PP servicer (resid 17):
  PP_AGCONOFF 1 (ensure), PP_AGCMAXGAIN 64 -> 16.0x, PP_ATTNS_MODE
  0 -> 1 (gain stays down in non-speech: the direct counter to the
  ride-up), PP_LIMITONOFF 1 with PP_LIMITPLIMIT 0.5 (output limiter,
  XMOS's anti-clip stage), PP_MIN_NS / PP_MIN_NN raised to a 0.05
  floor only if lower (never deepened). Read-and-log anchors, never
  written: PP_AGCDESIREDLEVEL (anchors the output levels the wake
  gate trained against), PP_AGCGAIN, AUDIO_MGR_MIC_GAIN (coupled to
  the AEC tuning). AGC time constants untouched per XMOS guidance.
  No SAVE_CONFIGURATION: the values are re-applied at every boot and
  the tuning vanishes with the call site -- trivially reversible.
- Alarm priority (main/alerts.{c,h}, main/sat_local.c, main/voice.{c,h}).
  alerts gains a fire hook, invoked from fire_locked (alarm, timer,
  and snooze refire all pass through it). sat_local registers
  on_alert_fire before alerts_start(): if voice_call_active(), it
  latches voice_request_end_call(). voice.c's run_call consumes the
  latch in its existing hangup path (reason "alert firing") with a
  shortened 2 s end-confirm wait (vs 20 s) so the speaker is freed
  fast; sat_local's speaker task then owns the alert at the alarm
  volume as before. The local-playback half of the requirement was
  already sat4 behavior (unducked alert owns the speaker, downlink
  dropped; ducked mixes). Gate-stop and ducking are untouched.
- Call greeting (devices/sdkconfig.lyra-satgadget):
  CONFIG_MUSE_REALTIME_VOICE_GREETING=y -- headless box, the greeting
  is the only sign a call connected. Verified in the GENERATED
  build sdkconfig (the standing post-config-edit check).
- Build: clean build in the mirror (build dir deleted for the
  sdkconfig change), exit 0, zero compiler warnings in changed
  files (two generic first-configure CMake notices only). Images:
  bootloader 0x0, partition table 0x10000, phy_init_data 0x1d000,
  muse-gadget.bin 0x20000 = 1,904,640 B signed (same padded size as
  sat5; unsigned 1,900,544 B). Binary carries version
  2026.10.31-sat6 and the new log strings. NOT flashed -- awaiting
  Daniel's review + USB; first boot's console log is the XVF
  verification (before values should read PP_AGCMAXGAIN 64.0).

## sat6b changes (2026-10-31, one-variable A/B of sat6: PP_ATTNS_MODE back to 0)

Version 2026.10.31-sat6b. Backup `.pre-sat6b` (voice_board_lyra_flex.c,
version.txt, PROVENANCE.md). sat6 was flashed 2026-10-31 and its first
live test (18:14 EDT) split the original failure in two: the anti-clip
half of the XVF tuning worked -- Daniel's test sentence ("Hey Lyra,
turn on my 7am alarm") arrived with ZERO rail samples and healthy
onset levels, vs 42-109 rail samples on sat5 -- but the stream dropped
to room floor (~140 RMS) ~0.8-1.0 s into the sentence and stayed there;
Whisper received only "Hey Lyra, turn". Of sat6's four tuning writes,
only PP_ATTNS_MODE has "reduce gain when it judges non-speech" as its
designed function (with factory ATTNS NOMINAL/SLOPE, flagged unmeasured
in the sat6 entry); a misjudged mid-sentence pause plus a slow AGC
release matches the symptom exactly. The other three cannot mute:
PP_AGCMAXGAIN only lowers a ceiling the onset never reached, the
limiter at 0.5 power never engages at ~0.03 peak power, and the raised
NS floors can only weaken suppression.

- Delta (main/voice_board_lyra_flex.c only): in xvf_apply_tuning(),
  PP_ATTNS_MODE is written 0 instead of 1. The write is kept (not
  deleted) so the boot before/after log proves the state even if the
  XVF held sat6's value across the reset. Everything else is sat6
  unchanged: PP_AGCMAXGAIN 16.0x, limiter on at PP_LIMITPLIMIT 0.5,
  PP_MIN_NS / PP_MIN_NN >= 0.05 floors, alarm priority, call greeting
  (verified =y in the generated build sdkconfig).
- Build: clean build in the mirror, exit 0, zero compiler warnings.
  Images: bootloader 0x0, partition table 0x10000, phy_init_data
  0x1d000, muse-gadget.bin 0x20000 = 1,904,640 B signed (same padded
  size as sat6). Binary carries version 2026.10.31-sat6b and the
  sat6b tune log string. NOT flashed -- awaiting Daniel's go-ahead;
  the boot log (XVF tune after: PP_ATTNS_MODE = 0) plus a repeat of
  the same test sentence is the A/B proof.

## sat6c changes (2026-10-31, AGC off + fixed gain; ATTNS exonerated; deferred DSP report)

Version 2026.10.31-sat6c. Backups `.pre-sat6c` (voice_board_lyra_flex.c,
version.txt, PROVENANCE.md). sat6b was flashed 2026-10-31 and its live
test produced the SAME failure as sat6: ~0.8-1.0 s of healthy,
un-clipped speech (zero rail samples), then the stream dropped to
room floor (~140-250 RMS) for the rest of the sentence while Daniel
was still speaking; Whisper received "Hey, Lyra, Charles." That A/B
result exonerates PP_ATTNS_MODE (identical failure with it on in sat6
and off in sat6b). The one stage left whose dynamics fit every
observed failure (sat5 quiet-room ride-up into the rail; sat6/sat6b
post-onset dive with a seconds-long recovery) is the AGC itself: its
time constants run to seconds (guide defaults AGCTIME 0.9 /
AGCFASTTIME 0.6) and its fast-alpha path engages on overshoot against
the untouched factory PP_AGCDESIREDLEVEL. sat6c removes the servo
from the chain instead of trimming it.

- Delta (main/voice_board_lyra_flex.c only):
  - PP_AGCONOFF 1 -> 0 (AGC adaptation off), written FIRST so the
    servo cannot fight the set-point write.
  - PP_AGCGAIN (17/13) written to 8.0x fixed. Per the XMOS tuning
    guide (XM-014888-PC, AGC Configuration): PP_AGCGAIN "is always
    applied, regardless of the value of PP_AGCONOFF"; AGCONOFF only
    controls whether the value may change -- a written AGCGAIN with
    adaptation off is the documented fixed-gain set-point (the
    guide's own flow records a converged AGCGAIN as the product
    default). The tune helper's before/after logging is the
    read-back proof. 8.0x (+18 dB) is the mid-band starting point:
    the proven clean passes peaked 1.3k-10k RMS output with the old
    AGC converged in the single digits to 16x, and Seeed's own boot
    default for AGCGAIN is 2.0x; a whisper now gets a fixed +18 dB
    instead of a ride to the ceiling, and the 0.5-power limiter
    still guards the rail.
  - Unchanged from sat6b: PP_AGCMAXGAIN 16.0 (inert with the AGC
    off; kept as the ceiling if adaptation is ever re-enabled),
    PP_ATTNS_MODE written 0 (no effect with AGCONOFF 0 per the
    guide; state kept explicit), limiter on at PP_LIMITPLIMIT 0.5,
    PP_MIN_NS / PP_MIN_NN >= 0.05 floors, alarm priority, call
    greeting.
  - Deferred DSP report (observability): across every sat6/sat6b
    boot, ZERO "XVF tune" lines reached the console capture -- the
    device re-enumerates USB on reset and the early-boot logs are
    gone before the capture `cat` attaches, so the applied values
    were never directly observed. sat6c keeps the boot read/log
    block and logs the same read-back set again ~10 s after boot
    from a one-shot task (xvf_report_task, created in
    voice_board_init after the tuning is applied) with the prefix
    "XVF report (deferred):": PP_AGCONOFF, PP_AGCMAXGAIN,
    PP_AGCDESIREDLEVEL, PP_AGCGAIN, PP_LIMITONOFF, PP_LIMITPLIMIT,
    PP_MIN_NS, PP_MIN_NN, PP_ATTNS_MODE. PP_AGCGAIN in that report
    doubles as proof the fixed gain held (it cannot drift with the
    AGC off). The link heartbeat was NOT extended (appending
    PP_AGCGAIN would need cross-component XVF reads from app.c,
    not a one-line format change).
- Build: clean build in the mirror (build dir deleted), exit 0,
  zero compiler warnings in the changed file. Images: bootloader
  0x0, partition table 0x10000, phy_init_data 0x1d000,
  muse-gadget.bin 0x20000 = 1,904,640 B signed (same padded size
  as sat6/sat6b), sha256
  e543e8a5c5f5149d1f14e1487c62a15a23c5453e7ff977a593a02f6b24d18b6b.
  Binary carries version 2026.10.31-sat6c, the "XVF report
  (deferred)" strings, the sat6c tune log string, and the
  alarm-priority strings; generated build sdkconfig verified
  CONFIG_MUSE_REALTIME_VOICE_GREETING=y. NOT flashed --
  awaiting Daniel's go-ahead; the A/B proof is the deferred
  report in the console capture (PP_AGCONOFF = 0, PP_AGCGAIN =
  8.000) plus a repeat of the test sentence surviving whole.

## sat6d -- 2026-10-31 (Daniel's directive: all mic settings back to stock)

- Daniel rejected the whole sat6/sat6b/sat6c XVF tuning line: the
  regression he cares about came from the Fedora service capture
  rewrites, not the DSP, and he ordered every mic setting
  returned to how it was. sat6c (AGC off + fixed gain + deferred
  DSP report) is SHELVED, never flashed; its voice_board state
  is preserved byte-for-byte in
  main/voice_board_lyra_flex.c.pre-sat6d (sha256 9eecd9ea...).
- The revert: main/voice_board_lyra_flex.c restored byte-for-byte
  from .pre-sat6 (sha256
  e9d185e46f143034367d342ddb2cffef90e66da77255bb7ef0528f2d66cd6ca2).
  That is exactly the sat5 audio path -- "stock" here means the
  XVF3800 runs its own configuration and the firmware's ONLY
  XVF control write is the boot unmute (GPO servicer, resid 20,
  xvf_unmute()); there is no xvf_apply_tuning and no PP_* DSP
  parameter write of any kind.
- Kept from sat6b, untouched, because none of it is mic-related:
  alarm priority (an alert fire ends an active call; binary
  still carries the "alert firing" strings) and the call
  greeting (generated sdkconfig re-verified
  CONFIG_MUSE_REALTIME_VOICE_GREETING=y).
- Build: clean build in the mirror (build dir deleted), exit 0,
  zero compiler warnings (the only "Warning" lines are the two
  stock ESP-IDF CMake notices present in every build). Images:
  bootloader 0x0, partition table 0x10000, phy_init_data 0x1d000,
  muse-gadget.bin 0x20000 = 1,904,640 B signed (same padded size
  as sat6/sat6b/sat6c), sha256
  2abb1e2377f8506ae3d6e043ef5cf4b7457676d693b356db927991a4d444742d.
  Binary string check: version 2026.10.31-sat6d present;
  "XVF tune" ABSENT; "XVF report (deferred)" ABSENT;
  "alert firing" present. NOT flashed -- awaiting Daniel's
  go-ahead. The A/B proof after a flash: the satellite is back
  on the exact sat5 mic configuration, so any remaining wake
  chopping is the service's, full stop.
