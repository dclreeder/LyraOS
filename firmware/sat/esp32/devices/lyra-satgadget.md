# Lyra Sat Gadget — XIAO ESP32S3 + ReSpeaker Flex

Headless voice satellite: no display, no buttons in product use, constant
power. Full product spec: `~/Documents/Lyra Sat Gadget/DESIGN.md`.

## Build

  . ~/esp/esp-idf-v6/export.sh
  tools/board.sh lyra-satgadget build

  (From a MIRROR at a space-free path -- see PROVENANCE.md "BUILD PATH
  GOTCHA": this project's own path has two spaces and breaks one ESP-IDF
  build step. The mirror recipe there is the standing build procedure.)

(Profile: sdkconfig.defaults + devices/sdkconfig.lyra-satgadget.
No SDK token is compiled in for the spike; first provisioning is a later,
separately-approved step.)

## Hardware facts (from the archived satellite firmware, which ran this board)

- XIAO ESP32S3: 8 MB flash, 8 MB octal PSRAM. Console/flashing over its own
  USB Serial/JTAG (303a:1001).
- XVF3800 control: I2C SDA GPIO5 / SCL GPIO6, address 0x2C. It boots muted;
  firmware un-mutes via GPO servicer {resid 20, cmd 1, len 2, gpio 30,
  value 0} before I2S init, then waits 500 ms.
- I2S (one shared bus): XVF is clock master at 48 kHz, XIAO slave,
  32-bit Philips stereo. BCLK GPIO8, WS GPIO7, DIN (mic) GPIO43,
  DOUT (playback) GPIO44. Left slot = beam 0 processed channel.
- No AIC3204 codec (that is the reSpeaker Lite, a different board): the
  Flex amp is fed from the XVF3800 DAC. Volume is a device-side gain.

## Open items before first power-on

- Which I2S firmware image the Flex's XVF3800 actually runs (the archived
  build assumed the 48 kHz master image; verify with xvf_host).
- Whether the echoed-audio channel claim (voice_board_echo_cancelled =
  false today) can be proven for full-duplex calls.
- Pairing identity / SDK token for this second gadget.
