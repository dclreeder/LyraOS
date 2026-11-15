/* Lyra Sat Gadget -- local-path LAN client (sat1).
 *
 * The device side of docs/local-path-transport.md: a TCP client to the
 * Fedora lyra-satgadget service (archived satellite framing, TCP 9876),
 * streaming the Flex mic channel up and playing downlink PCM through
 * voice_board_lyra_flex, plus the CTRL dispatcher (hello / ping /
 * barge_in / set_volume / get_status / start_call). start_call hands
 * the escalation to the SDK realtime call engine via voice.c's
 * voice_request_call(); while a call is active the local path yields
 * the mic and speaker to it.
 */
#pragma once

#include <stdbool.h>

/* Start the local-path tasks (net, mic, speaker, call monitor).
 * Idempotent; call once after voice_init(). The net task gates its
 * first connect on the station holding a real IP (GOT_IP). */
void sat_local_start(void);

/* True while the TCP session to the Fedora service is up. */
bool sat_local_connected(void);
