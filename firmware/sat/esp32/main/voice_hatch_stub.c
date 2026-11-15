/* Lyra local satellite (sat8): inert stand-ins for the Muse cloud
 * voice-session API (muse_chat.h).
 *
 * This box is a direct local satellite: Fedora is the brain, and the
 * box never opens a Muse cloud session. The SDK's voice.c still
 * carries its push-to-talk "hatch" call sites, so the API is
 * satisfied here with stubs that report not-ready and do nothing --
 * which keeps libmuse's chat session, account API and MP3 decoder
 * out of the binary entirely (see components/muse/CMakeLists.txt,
 * the CONFIG_HOMEHUB_VOICE branch).
 */
#include "muse_chat.h"

void muse_hatch_start(void) {}
bool muse_hatch_ready(void) { return false; }
void muse_hatch_turn_begin(void) {}
void muse_hatch_turn_audio(const int16_t *pcm, size_t frames)
{
    (void)pcm;
    (void)frames;
}
void muse_hatch_turn_end(void) {}
void muse_hatch_turn_cancel(void) {}
muse_hatch_ev_t muse_hatch_turn_event(char *text, size_t cap)
{
    if (text && cap) text[0] = '\0';
    return MUSE_HATCH_EV_NONE;
}
size_t muse_hatch_turn_read(int16_t *pcm, size_t frames, int wait_ms)
{
    (void)pcm;
    (void)frames;
    (void)wait_ms;
    return 0;
}
