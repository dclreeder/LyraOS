#!/bin/bash
export XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
LLAMA=$HOME/Documents/LyraOS/modules/llm/llama.cpp/build/bin/llama-server
PY=$HOME/Documents/esphome-probe/venv/bin/python
TEST=$HOME/Documents/LyraOS/benches/curator_synthetic_test.py
OUT=$HOME/Documents/LyraOS/benches
M=$HOME/Documents/LyraOS/data/models
health () { curl -s http://127.0.0.1:8090/health | grep -q '"ok"'; }
wait_health () { for i in $(seq 1 48); do health && return 0; sleep 5; done; return 1; }
start_server () { # $1 gguf $2 ngl
  systemctl --user stop lyra-cand-server 2>/dev/null; sleep 3
  systemd-run --user --unit=lyra-cand-server --collect /bin/bash -c "$LLAMA -m $1 --host 127.0.0.1 --port 8090 -ngl $2 -c 65536 --alias nemotron-nano-12b-v2 --jinja --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 > $OUT/cand_server.log 2>&1"
}
run_one () { # $1 label $2 gguf $3 ngl
  start_server "$2" "$3"
  if ! wait_health; then
    if [ "$3" -gt 20 ]; then start_server "$2" 20; wait_health || { echo "LOAD-FAILED $1" > $OUT/cand_$1.log; return; }
    else echo "LOAD-FAILED $1" > $OUT/cand_$1.log; return; fi
  fi
  $PY $TEST > $OUT/cand_$1.log 2>&1
  systemctl --user stop lyra-cand-server 2>/dev/null; sleep 3
}
systemctl --user stop lyra-curator-server8 2>/dev/null; sleep 3
run_one 30b $M/nvidia_Nemotron-3-Nano-30B-A3B-Q4_K_M.gguf 26
run_one qwen $M/Qwen3.5-9B-Q8_0.gguf 99
run_one 12b $M/NVIDIA-Nemotron-Nano-12B-v2-Q8_0.gguf 99
echo CHAIN-DONE > $OUT/cand_chain_done.txt
