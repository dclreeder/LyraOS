/*
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// Push-to-talk voice chat with the agent. Hold the button and speak; on
// release the recording goes to the agent's chat as a voice note, which the
// VM transcribes, over the voice session borrowed from Muse (muse_chat.h).
// The reply is text and shows up in the Muse app; a TTS API of your own can
// speak it (start_tts in muse_chat_session.cpp).

#pragma once

#include <stdbool.h>

#include "cJSON.h"

// Start the voice task. It brings up the audio hardware and then takes over
// the button whenever a turn can run.
void voice_init(void);

// voice.configure: sets the speaker volume (0-100), kept in NVS. The dial on
// top sets it too.
cJSON *voice_configure_command(cJSON *params);

// Lyra Sat Gadget local path (sat1, main/sat_local.c): request a realtime
// call from the network side (CTRL start_call). The voice task runs the
// call when the session is ready; a request made while it is not is
// dropped and logged. voice_call_active() is true while a call runs on
// the voice task (whichever way it was started). voice_ready() is true
// once the audio hardware is up.
void voice_request_call(void);
// Lyra Sat Gadget (sat6, alarm priority): latch a request for the voice
// task to end the running call (an alert is firing and must ring at
// the alarm volume). A no-op when no call is running; consumed by
// run_call, which clears it at call start.
void voice_request_end_call(void);
bool voice_call_active(void);
bool voice_ready(void);
