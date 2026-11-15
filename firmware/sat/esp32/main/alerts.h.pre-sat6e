/* Lyra Sat Gadget -- on-device alarm + timer engine (sat4).
 *
 * Modeled on the AMOLED gadget's proven muse_alarms semantics (NVS-
 * persisted alarms, once/daily/weekdays repeats, 500 ms tick against
 * local time, snooze +9 min RAM-held, 10 min ring cap) and adapted to
 * this headless satellite:
 *   - storage is the SDK config_store (string values, NVS ns "homehub");
 *     the alarm table is one JSON document under the key "alarms_v1";
 *   - time comes from SNTP (started by sat_local once the station has
 *     an IP) with the TZ default EST5EDT,M3.2.0/2,M11.1.0 (US Eastern);
 *   - the alert sound is the embedded HomePod alarm WAV (16 kHz mono),
 *     resampled to the 48 kHz bus and looped while alerting, played by
 *     sat_local's speaker task through alerts_render_frame();
 *   - timers are RAM-held countdowns (max 4), never persisted; snooze
 *     applies to alarms only -- a fired timer is dismissed with stop.
 *
 * Threading rules (the standing PSRAM rule): the tick runs on a PSRAM
 * stack and NEVER writes flash -- tick-originated persistence goes
 * through an internal-stack store task. CTRL-originated mutations run
 * on sat_local's net task (internal stack), which already owns NVS
 * writes for set_volume, and may write directly.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cJSON.h"

#define ALERTS_MAX_ALARMS 6
#define ALERTS_MAX_TIMERS 4
#define ALERTS_LABEL_MAX 24

#define ALERT_KIND_NONE 0
#define ALERT_KIND_ALARM 1
#define ALERT_KIND_TIMER 2

/* Playback snapshot for sat_local's speaker task. */
typedef struct {
    bool active;
    bool ducked;
    uint8_t kind; /* ALERT_KIND_* */
    uint8_t id;
    int64_t start_us;
} alerts_pb_t;

/* Fire hook (sat6, alarm priority): invoked synchronously from the
 * fire path (alert lock held, tick task) the moment any alert --
 * alarm, timer, or snooze refire -- starts ringing. The hook MUST be
 * non-blocking, must not touch flash, and must not call back into
 * the alerts API. Register before alerts_start(); the default NULL
 * disables it. */
typedef void (*alerts_fire_hook_t)(uint8_t kind, uint8_t id);
void alerts_set_fire_hook(alerts_fire_hook_t hook);

/* Load persisted alarms, parse the embedded WAV, start tick + store
 * tasks, set the TZ default. Call once, before the CTRL path runs. */
void alerts_start(void);

/* Start SNTP (idempotent). Call when the station first holds an IP. */
void alerts_time_sync_start(void);

/* Link up/down notification: losing the link while ducked restores
 * full alert volume immediately (a ducked alarm never stays quiet). */
void alerts_set_connected(bool connected);

/* CTRL verbs (called from sat_local's net task). Mutations return
 * true when applied; state is read back with alerts_build_state_json. */
bool alerts_cmd_set_alarm(const cJSON *root);    /* alarms_set */
bool alerts_cmd_delete_alarm(int id);            /* alarms_delete */
bool alerts_cmd_enable_alarm(int id, bool en);   /* alarms_enable */
bool alerts_cmd_set_timer(const cJSON *root);    /* timers_set */
bool alerts_cmd_cancel_timer(int id);            /* timers_cancel */
void alerts_cmd_stop(void);                      /* alert_stop */
void alerts_cmd_snooze(void);                    /* alert_snooze */
/* Duck the alert to 25% of the alarm volume (barge_in while alerting
 * routes here). Returns true when an alert is active. */
bool alerts_cmd_duck(void);                      /* alert_duck / barge_in */
void alerts_cmd_duck_end(void);                  /* alert_duck_end */

/* State: dirty flag is set on any change (fire, stop, snooze, cap,
 * mutation) so sat_local can push alerts_state upstream. */
bool alerts_take_state_dirty(void);
bool alerts_build_state_json(char *out, size_t cap);

/* Speaker-task side: snapshot + render one 48 kHz mono frame of the
 * alert sound at the given effective gain percent. Render calls must
 * come from a single task (sat_local's speaker task). */
alerts_pb_t alerts_playback_snapshot(void);
bool alerts_render_frame(int16_t *out, size_t frames, int gain_pct);
