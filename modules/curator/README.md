# curator — the sleep-cycle memory curator model

NVIDIA Nemotron Nano 12B v2 (the 12B version of Lyra's own 9B
brain), Q8_0, thinking ON. Started by the core's sleep cycle on
127.0.0.1:8090 with a 64K context, alone in VRAM; it is never a
standing service. The thinking-enabled 9B is the standing
fallback (same pipeline, lyra_core.curator is model-agnostic).

## How the GGUF was made (2026-11-21)

Converted HERE from NVIDIA's official BF16 checkpoint
(huggingface.co/nvidia/NVIDIA-Nemotron-Nano-12B-v2; all six
shards sha256-verified against the repo tree's LFS oids after a
parallel ranged download -- download.py re-runs it). Output:
data/models/NVIDIA-Nemotron-Nano-12B-v2-Q8_0.gguf (13.1 GB).

Convert with modules/curator/convert.py, NOT the stock
convert_hf_to_gguf.py invocation. The stock path loads hparams
through transformers' AutoConfig, whose NemotronH config
materializes family defaults the raw config.json does not
contain -- including num_experts_per_tok=2. The converter
decides MoE-vs-dense by that key's presence, so the stock path
stamps this dense model nemotron_h_moe and the runtime refuses
to load it ("blk.1.ssm_in.weight not found"). convert.py feeds
the converter the raw config.json instead -- the converter's
own fallback for un-loadable configs -- and the GGUF comes out
nemotron_h, matching NVIDIA's official 9B GGUF. Neither the
checkpoint nor llama.cpp is modified.

## Measured footprint (2026-11-21, this box)

Curator server (-ngl 99, -c 65536, q8_0 KV, flash attention,
unified KV) + the embedding service + the desktop: 15.8 GiB of
the 16 GiB card. It fits with no offload and ~0.2 GiB spare --
the tightest constraint in the design; a larger future curator
quant or context will need this re-measured first.
