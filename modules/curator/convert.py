"""Convert the official Nemotron Nano 12B v2 checkpoint to GGUF
with llama.cpp's converter, feeding it the RAW config.json as
the hparams.

Why the wrapper: conversion.base.ModelBase.load_hparams prefers
AutoConfig.from_pretrained().to_dict(), and current transformers
materializes Nemotron-3-family DEFAULTS into that dict --
including num_experts_per_tok=2, a key the raw config.json does
not contain (this checkpoint is dense). NemotronHModel decides
MoE-vs-dense by the key's mere presence, so the stock path
stamps the GGUF nemotron_h_moe and the runtime refuses its
tensors. The converter's own fallback for un-loadable configs
is the raw config.json; this wrapper takes that fallback
deliberately. Checkpoint files and llama.cpp sources are both
untouched.
"""
import json
import runpy
import sys
from pathlib import Path

LLAMA = Path("~/Documents/LyraOS/modules/llm/llama.cpp").expanduser()
sys.path.insert(0, str(LLAMA))

from conversion.base import ModelBase  # noqa: E402


def _raw_load(dir_model, is_mistral_format, guess=True):
    if is_mistral_format:
        with open(Path(dir_model) / "params.json", encoding="utf-8") as f:
            return json.load(f)
    with open(Path(dir_model) / "config.json", encoding="utf-8") as f:
        return json.load(f)


ModelBase.load_hparams = staticmethod(_raw_load)

sys.argv = ["convert_hf_to_gguf.py",
            str(Path("~/Documents/LyraOS/modules/curator/hf").expanduser()),
            "--outfile", str(Path(
                "~/Documents/LyraOS/data/models/"
                "NVIDIA-Nemotron-Nano-12B-v2-Q8_0.gguf").expanduser()),
            "--outtype", "q8_0"]
runpy.run_path(str(LLAMA / "convert_hf_to_gguf.py"), run_name="__main__")
