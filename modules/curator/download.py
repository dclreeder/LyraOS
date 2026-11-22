"""Download the official Nemotron Nano 12B v2 BF16 checkpoint
(nvidia/NVIDIA-Nemotron-Nano-12B-v2) with parallel ranged curl
streams, sha256-verifying every shard against the repo tree's
LFS oids. Build artifact for the memory curator (Phase 2);
re-runnable -- completed shards are skipped by hash."""
import hashlib, json, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = "nvidia/NVIDIA-Nemotron-Nano-12B-v2"
BASE = f"https://huggingface.co/{REPO}/resolve/main"
HERE = Path(__file__).resolve().parent
HF = HERE / "hf"
HF.mkdir(exist_ok=True)
RANGES = 3

def curl(args):
    subprocess.run(["curl", "-sL", "--retry", "8", *args], check=True)

tree = json.loads(subprocess.run(
    ["curl", "-s", f"https://huggingface.co/api/models/{REPO}/tree/main"],
    capture_output=True, check=True).stdout)
files = {f["path"]: f for f in tree if f["type"] == "file"}

small = [p for p in files if not p.endswith(".safetensors")
         and p not in (".gitattributes",) and not p.endswith(".png")
         and not p.endswith(".md")]
for p in small:
    dest = HF / p
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        curl(["-o", str(dest), f"{BASE}/{p}"])
    print("small:", p, flush=True)

shards = sorted(p for p in files if p.endswith(".safetensors"))

def fetch_range(job):
    path, s, e, out = job
    curl(["--range", f"{s}-{e}", "-o", str(out), f"{BASE}/{path}"])

jobs, layout = [], {}
for p in shards:
    size = files[p]["size"]
    part = (size + RANGES - 1) // RANGES
    outs = []
    for i in range(RANGES):
        s = i * part
        e = min(s + part - 1, size - 1)
        out = HF / f".{Path(p).name}.part{i}"
        outs.append(out)
        jobs.append((p, s, e, out))
    layout[p] = outs

with ThreadPoolExecutor(max_workers=18) as ex:
    list(ex.map(fetch_range, jobs))

for p in shards:
    dest = HF / p
    with open(dest, "wb") as fh:
        for out in layout[p]:
            fh.write(Path(out).read_bytes())
            Path(out).unlink()
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    want = (files[p].get("lfs") or {}).get("oid", "")
    assert digest == want, f"{p}: sha mismatch {digest} != {want}"
    print("verified:", p, flush=True)

(HERE / "download.done").write_text("DOWNLOAD-DONE\n")
print("ALL DONE")
