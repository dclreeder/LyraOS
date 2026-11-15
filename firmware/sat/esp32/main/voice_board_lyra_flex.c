// voice_board implementation for the Lyra Sat Gadget: Seeed XIAO ESP32S3 +
// ReSpeaker Flex (XVF3800). Headless satellite; see devices/lyra-satgadget.md.
//
// The Flex is NOT the reSpeaker Lite: there is no AIC3204 codec (the speaker
// amp is fed by the XVF3800's own DAC), and the XVF3800 answers I2C control
// at 0x2C, not 0x42. Facts below come from the archived satellite firmware
// (~/Documents/_archive-20261030/Lyra Box/firmware, generated sdkconfig +
// main.c/audio.c), which ran this exact board:
//
//   - I2C control bus: SDA GPIO5, SCL GPIO6; XVF3800 at 0x2C.
//   - The XVF3800 BOOTS MUTED: unmute = GPO servicer write resid=20, cmd=1
//     (WRITE_VALUE), len=2, {gpio=30, value=0}, BEFORE I2S init; then give
//     its clock 500 ms to settle.
//   - One shared I2S bus (I2S_NUM_0), XVF is clock master at 48 kHz
//     (CONFIG_SAT_XVF_MASTER_48K in the archived build), XIAO is slave;
//     Philips, 32-bit stereo slots. BCLK GPIO8, WS GPIO7,
//     DIN (mic, XVF -> XIAO) GPIO43, DOUT (playback, XIAO -> XVF) GPIO44.
//     (The Lite board file wires din/dout the other way round; the archived
//     Flex build's generated sdkconfig is the ground truth for THIS board.)
//   - Mic: LEFT slot = beam 0, the XVF's processed channel; int16 sample is
//     slot >> 16; the archived firmware decimates 48k -> 16k by 3:1
//     averaging. Playback upsampled to the 48 kHz bus there too.
//
// sat7 (2026-11-11): the Flex DOES carry a TLV320AIC3104 codec on the
// same I2C bus (address 0x18) -- proven on this exact board by the
// ESPHome build's register readbacks (module power status reg 0x5E).
// Its power-on reset state leaves the DACs unpowered, the channels
// muted and every output driver off, so the output path must be
// initialised explicitly or the speaker stays silent; the ESPHome
// build's bring-up (incl. the +9 dB output-level registers, its
// 2026-11-10 loudness fix) is ported in aic3104_init_output_path()
// below. (The "no codec" claim above came from the archived
// firmware's docs and is wrong for this board.)
//
// This board speaks the bus natively at 48 kHz, which is exactly
// VOICE_SPEAKER_RATE, so playback needs no resampling; the mic path does
// the proven 3:1 averaging decimation down to VOICE_MIC_RATE.
//
// Volume: applied as a software gain in speaker_write (never above unity).
// The product volume rule stands: the persistent, user-facing volume lives
// on the DEVICE (the archived firmware kept it in NVS and applied it to TX
// PCM); this board-level gain stage is where the SDK's Link-layer volume
// lands, and it never boosts.
#include "voice_board.h"

#include <stdlib.h>
#include <string.h>

#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "voice_flex";

#define XVF_I2C_ADDR 0x2C
#define AIC3104_I2C_ADDR 0x18
#define I2C_SDA_GPIO 5
#define I2C_SCL_GPIO 6

#define I2S_BCLK_GPIO 8
#define I2S_WS_GPIO 7
#define I2S_DOUT_GPIO 44 // XIAO -> XVF (playback)
#define I2S_DIN_GPIO 43  // XVF -> XIAO (mic)

#define BUS_RATE VOICE_SPEAKER_RATE // 48000: the XVF master clock rate
#define MIC_CHUNK 320               // capture chunk: VOICE_MIC_RATE / 50 (voice.c)
#define MIC_DECIM 3                 // 48k -> 16k (BUS_RATE / VOICE_MIC_RATE)

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_xvf;
static i2s_chan_handle_t s_tx, s_rx;
static bool s_mic_on;
static bool s_unmuted; // XVF control answered + unmute sent
static int s_volume_pct = 100;
static SemaphoreHandle_t s_lock;
// One output chunk's worth of raw bus frames: 320 mic frames * 3 decim *
// 2 slots. Static: speaker_write runs in the small-stacked player task.
static int32_t s_raw[MIC_CHUNK * MIC_DECIM * 2];
static int32_t s_out[64 * 2];
//
// XVF3800 control (resid/cmd/len framing, ctl protocol v2).
//
static esp_err_t xvf_probe(void)
{
    if (!s_bus)
        return ESP_ERR_INVALID_STATE;
    if (i2c_master_probe(s_bus, XVF_I2C_ADDR, 100) != ESP_OK)
        return ESP_ERR_NOT_FOUND;
    if (s_xvf)
        return ESP_OK;
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = XVF_I2C_ADDR,
        .scl_speed_hz = 100000,
    };
    return i2c_master_bus_add_device(s_bus, &cfg, &s_xvf);
}

// resid=20 (GPO servicer), cmd=1 (WRITE_VALUE), len=2, {gpio=30, value=0}.
static esp_err_t xvf_unmute(void)
{
    const uint8_t cmd[] = {20, 1, 2, 30, 0};
    return i2c_master_transmit(s_xvf, cmd, sizeof(cmd), 100);
}

// Best-effort GETVERSION (resid=240, cmd=88 read): log only, never fatal.
static void xvf_log_version(void)
{
    const uint8_t cmd[] = {240, (88 | 0x80), 0};
    uint8_t resp[32] = {0};
    if (i2c_master_transmit(s_xvf, cmd, sizeof(cmd), 100) == ESP_OK &&
        i2c_master_receive(s_xvf, resp, sizeof(resp), 100) == ESP_OK) {
        ESP_LOGI(TAG, "XVF3800 version resp: %02x %02x %02x %02x",
                 resp[0], resp[1], resp[2], resp[3]);
    } else {
        ESP_LOGW(TAG, "XVF3800 version read failed (non-fatal)");
    }
}

/* --- TLV320AIC3104 output-path bring-up (sat7 port of the ESPHome
 * build's on_boot init, which was itself grounded in the TI SLAS510C
 * register tables and proven on this board). Page 0 throughout.
 * Routing powers ALL FOUR output pairs: which pair feeds the board's
 * amplifier is undocumented, and powering an unconnected output is
 * harmless. Level registers carry 0x99: output level +9 dB (the
 * 2026-11-10 loudness fix), unmuted, powered. DAC digital volume is
 * set to 0 dB (0x00): the sat stack applies the user volume as a PCM
 * gain in speaker_write, so the codec stage stays at unity.
 * Best-effort: a missing codec logs a warning and init continues. */
static void aic3104_init_output_path(void)
{
    if (i2c_master_probe(s_bus, AIC3104_I2C_ADDR, 100) != ESP_OK) {
        ESP_LOGW(TAG, "AIC3104 not answering at 0x%02x; "
                      "speaker output path NOT initialised",
                 AIC3104_I2C_ADDR);
        return;
    }
    i2c_master_dev_handle_t dev = NULL;
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = AIC3104_I2C_ADDR,
        .scl_speed_hz = 100000,
    };
    if (i2c_master_bus_add_device(s_bus, &cfg, &dev) != ESP_OK || !dev) {
        ESP_LOGW(TAG, "AIC3104 could not be added to the I2C bus");
        return;
    }
    static const struct { uint8_t reg, val; } kWrites[] = {
        {0x00, 0x00}, /* page select 0 */
        {0x29, 0x00}, /* DAC output switching: L->DAC_L1, R->DAC_R1 */
        {0x25, 0xC0}, /* DAC power up: left + right */
        {0x2F, 0x80}, /* route DAC_L1 -> HPLOUT @ 0 dB */
        {0x40, 0x80}, /* route DAC_R1 -> HPROUT @ 0 dB */
        {0x36, 0x80}, /* route DAC_L1 -> HPLCOM @ 0 dB */
        {0x47, 0x80}, /* route DAC_R1 -> HPRCOM @ 0 dB */
        {0x52, 0x80}, /* route DAC_L1 -> LEFT_LOP/M @ 0 dB */
        {0x5C, 0x80}, /* route DAC_R1 -> RIGHT_LOP/M @ 0 dB */
        {0x33, 0x0F}, /* HPLOUT level 0 dB, unmuted, powered (sat9: was 0x99 = +9 dB) */
        {0x41, 0x0F}, /* HPROUT level 0 dB, unmuted, powered (sat9: was 0x99 = +9 dB) */
        {0x3A, 0x0F}, /* HPLCOM level 0 dB, unmuted, powered (sat9: was 0x99 = +9 dB) */
        {0x48, 0x0F}, /* HPRCOM level 0 dB, unmuted, powered (sat9: was 0x99 = +9 dB) */
        {0x56, 0x09}, /* LEFT_LOP/M level 0 dB, unmuted (sat9: was 0x99 = +9 dB) */
        {0x5D, 0x09}, /* RIGHT_LOP/M level 0 dB, unmuted (sat9: was 0x99 = +9 dB) */
        {0x2B, 0x00}, /* DAC left digital volume 0 dB, unmuted */
        {0x2C, 0x00}, /* DAC right digital volume 0 dB, unmuted */
    };
    int fails = 0;
    for (size_t i = 0; i < sizeof(kWrites) / sizeof(kWrites[0]); i++) {
        uint8_t frame[2] = {kWrites[i].reg, kWrites[i].val};
        if (i2c_master_transmit(dev, frame, sizeof(frame), 100) != ESP_OK)
            fails++;
    }
    uint8_t reg = 0x5E, pwr = 0;
    bool rb = i2c_master_transmit_receive(dev, &reg, 1, &pwr, 1, 100) == ESP_OK;
    ESP_LOGI(TAG, "AIC3104 output path up: %d/%d writes ok; power status "
                  "0x5E=%s0x%02X (DAC L:%d R:%d, LOP L:%d R:%d, HP L:%d R:%d)",
             (int)(sizeof(kWrites) / sizeof(kWrites[0])) - fails,
             (int)(sizeof(kWrites) / sizeof(kWrites[0])),
             rb ? "" : "?", pwr, (pwr >> 7) & 1, (pwr >> 6) & 1,
             (pwr >> 4) & 1, (pwr >> 3) & 1, (pwr >> 2) & 1, (pwr >> 1) & 1);
    /* sat9: read the output-level regs back too, so the log proves the
     * 0 dB staging (HP/COM 0x0F, LOP/M 0x09) actually landed. */
    static const uint8_t kLevelRegs[] = {0x33, 0x41, 0x3A, 0x48, 0x56, 0x5D};
    uint8_t lv[6] = {0xEE, 0xEE, 0xEE, 0xEE, 0xEE, 0xEE};
    for (size_t i = 0; i < sizeof(kLevelRegs); i++) {
        uint8_t r = kLevelRegs[i];
        i2c_master_transmit_receive(dev, &r, 1, &lv[i], 1, 100);
    }
    ESP_LOGI(TAG, "AIC3104 output levels: 0x33=0x%02X 0x41=0x%02X "
                  "0x3A=0x%02X 0x48=0x%02X 0x56=0x%02X 0x5D=0x%02X",
             lv[0], lv[1], lv[2], lv[3], lv[4], lv[5]);
}

esp_err_t voice_board_init(void)
{
    s_lock = xSemaphoreCreateMutex();

    i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = I2C_SDA_GPIO,
        .scl_io_num = I2C_SCL_GPIO,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags = {.enable_internal_pullup = true},
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &s_bus));

    // Unmute BEFORE I2S init so the XVF clock is stable when we lock on
    // (the archived firmware's sequence). No XIAO-side reset pulse: the
    // archived firmware never drove one, and the Flex's reset routing from
    // the XIAO is unknown -- do not invent wiring.
    if (xvf_probe() == ESP_OK) {
        s_unmuted = xvf_unmute() == ESP_OK;
        if (s_unmuted) {
            xvf_log_version();
        } else {
            ESP_LOGW(TAG, "XVF3800 unmute write failed");
        }
    } else {
        ESP_LOGW(TAG, "XVF3800 not answering at 0x%02x "
                      "(wrong I2S/control firmware on the XVF?)",
                 XVF_I2C_ADDR);
    }
    aic3104_init_output_path();

    vTaskDelay(pdMS_TO_TICKS(500)); // let the XVF clock settle

    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_SLAVE);
    chan_cfg.dma_desc_num = 6;
    chan_cfg.dma_frame_num = 480; // 10 ms at 48 kHz
    chan_cfg.auto_clear = true;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &s_tx, &s_rx));

    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(BUS_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT,
                                                        I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = I2S_BCLK_GPIO,
            .ws = I2S_WS_GPIO,
            .dout = I2S_DOUT_GPIO,
            .din = I2S_DIN_GPIO,
            .invert_flags = {0},
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_tx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(s_rx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(s_tx));

    ESP_LOGI(TAG, "Flex voice board up: XVF %s, I2S slave @%d, "
                  "mic decimate x%d",
             s_unmuted ? "unmuted" : "MUTE STATE UNKNOWN", BUS_RATE,
             MIC_DECIM);
    return ESP_OK;
}

esp_err_t voice_board_mic_start(void)
{
    if (s_mic_on)
        return ESP_OK;
    esp_err_t err = i2s_channel_enable(s_rx);
    if (err == ESP_OK)
        s_mic_on = true;
    return err;
}

void voice_board_mic_stop(void)
{
    if (!s_mic_on)
        return;
    i2s_channel_disable(s_rx);
    s_mic_on = false;
}

size_t voice_board_mic_read(int16_t *pcm, size_t frames, int *peak)
{
    int pk = 0;
    if (peak)
        *peak = 0;
    if (!s_mic_on || !pcm || frames == 0)
        return 0;
    if (frames > MIC_CHUNK)
        frames = MIC_CHUNK;

    const size_t bus_frames = frames * MIC_DECIM;
    const size_t want = bus_frames * 2 * sizeof(int32_t);
    size_t got = 0;
    while (got < want) {
        size_t n = 0;
        if (i2s_channel_read(s_rx, (uint8_t *)s_raw + got, want - got, &n,
                             500) != ESP_OK ||
            n == 0)
            break;
        got += n;
    }
    const size_t out = (got / (2 * sizeof(int32_t))) / MIC_DECIM;
    for (size_t i = 0; i < out; i++) {
        int32_t acc = 0;
        for (int d = 0; d < MIC_DECIM; d++)
            acc += s_raw[(i * MIC_DECIM + d) * 2] >> 16; // LEFT = beam 0
        int16_t v = (int16_t)(acc / MIC_DECIM);
        pcm[i] = v;
        int a = v < 0 ? -(int)v : (int)v;
        if (a > pk)
            pk = a;
    }
    if (peak)
        *peak = pk;
    return out;
}

esp_err_t voice_board_speaker_write(const int32_t *frames, size_t count)
{
    if (!frames || count == 0)
        return ESP_ERR_INVALID_ARG;
    const int64_t pct = s_volume_pct;
    while (count > 0) {
        size_t n = count > 64 ? 64 : count;
        for (size_t i = 0; i < n * 2; i++) {
            int64_t v = ((int64_t)frames[i] * pct) / 100;
            if (v > INT32_MAX)
                v = INT32_MAX;
            if (v < INT32_MIN)
                v = INT32_MIN;
            s_out[i] = (int32_t)v;
        }
        size_t written = 0;
        i2s_channel_write(s_tx, s_out, n * 2 * sizeof(int32_t), &written, 1000);
        size_t done = written / (2 * sizeof(int32_t));
        if (done == 0)
            return ESP_ERR_TIMEOUT; // TX stalled; drop the rest, never wedge
        frames += done * 2;
        count -= done;
    }
    return ESP_OK;
}

void voice_board_amp(bool on)
{
    // The Flex's speaker amp is fed by the XVF3800's DAC output; the
    // archived firmware has no XIAO-side amp-enable GPIO. Nothing to drive.
    (void)on;
}

bool voice_board_muted(void)
{
    // Fail closed like the SDK's Lite port: if the XVF never confirmed the
    // unmute, report muted rather than stream a silent room.
    return !s_unmuted;
}

bool voice_board_echo_cancelled(void)
{
    // The XVF3800 runs AEC inside its processed output, but this port does
    // not read the DSP pipeline status back, so the claim is unproven:
    // calls stay half-duplex until a live test says otherwise. Same
    // conservative stance as the SDK's reSpeaker Lite port.
    return false;
}

int voice_board_dial_steps(void) { return 0; }

void voice_board_set_volume(int percent)
{
    if (percent < 0)
        percent = 0;
    if (percent > 100)
        percent = 100;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_volume_pct = percent;
    xSemaphoreGive(s_lock);
}
