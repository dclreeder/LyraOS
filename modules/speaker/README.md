# Speaker marking

Whose voice is a turn? `core/lyra_core/speaker.py` (SpeakerGate)
embeds each routed turn's audio and compares it with Daniel's
enrolled voiceprint. Labels: daniel / guest / unknown -- attribution
for her context and the memory record, never access control.

Model: WeSpeaker ECAPA-TDNN 512 (VoxCeleb, large-margin), official
ONNX export from huggingface.co/Wespeaker/wespeaker-ecapa-tdnn512-LM
(`voxceleb_ECAPA512_LM.onnx`, sha256
d71b85d9b48058ef68004f04f1b78acebefb9dfcf542e19b976a12a5ad1f10b0),
stored at `data/speaker/`. Input is 80-dim kaldi-style log-mel
(fbank in speaker.py, shared by enrollment and runtime); output a
192-dim embedding.

Enrollment: `enroll.py` (this folder) averages embeddings over 149
of Daniel's filed wake-positive clips; held-out calibration
(2026-11-22): genuine held-out Daniel p1 0.437 / median 0.691;
negatives median 0.130, p99 0.632 (the tail is Daniel himself in
the rejected pool -- mumbling fails the wake check, not the voice).
Threshold 0.534 = mid-gap, genuine accepted 93.3%. Re-run enroll.py
to re-derive the voiceprint + threshold from the current pools.
