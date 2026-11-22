#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
LLAMA=$HOME/Documents/LyraOS/modules/llm/llama.cpp/build/bin/llama-server
PY=$HOME/Documents/LyraOS/core/venv/bin/python
TEST=$HOME/Documents/LyraOS/benches/curator_synthetic_test.py
OUT=$HOME/Documents/LyraOS/benches
M=$HOME/Documents/LyraOS/data/models
export CURATOR_SYSTEM_FILE=$OUT/prompt_middle.txt
systemctl --user stop lyra-llm.service; sleep 2
systemd-run --user --unit=lyra-cand-server --collect /bin/bash -c "$LLAMA -m $M/nvidia_Nemotron-3-Nano-30B-A3B-Q4_K_M.gguf --host 127.0.0.1 --port 8090 -ngl 26 -c 65536 --alias nemotron-nano-12b-v2 --jinja --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 > $OUT/cand_server.log 2>&1"
for i in $(seq 1 48); do curl -s http://127.0.0.1:8090/health | grep -q '"ok"' && break; sleep 5; done
$PY $TEST > $OUT/cand_middle30.log 2>&1
systemctl --user stop lyra-cand-server 2>/dev/null; sleep 3
systemctl --user start lyra-llm.service; systemctl --user restart lyra-core.service
sleep 14; curl -s http://127.0.0.1:8094/health > $OUT/cand_middle_health.txt
echo DONE > $OUT/cand_middle_done.txt
