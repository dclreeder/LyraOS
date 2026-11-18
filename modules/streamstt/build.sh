#!/bin/bash
# Build the Nemotron streaming sidecar against the parakeet.cpp
# build tree (~/Documents/parakeet.cpp). Output lands in build/
# (gitignored) like the bench harness before it.
set -euo pipefail
cd "$(dirname "$0")"
PK="$HOME/Documents/parakeet.cpp"
GGML_SRC="$PK/build/third_party/ggml/src"
mkdir -p build
cc -O2 -std=c11 -Wall -Wextra -Wno-unused-parameter \
  -I"$PK/include" \
  nemotron_stream_server.c -o build/nemotron-stream-server \
  "$PK/build/libparakeet.a" \
  "$PK/build/third_party/ced.cpp/libced.a" \
  "$PK/build/third_party/voice-detect.cpp/libvoicedetect.a" \
  -L"$GGML_SRC" -L"$GGML_SRC/ggml-vulkan" \
  -lggml -lggml-cpu -lggml-vulkan -lggml-base \
  -Wl,-rpath,"$GGML_SRC:$GGML_SRC/ggml-vulkan" \
  -lm -lstdc++ -lpthread -ldl
echo "built: build/nemotron-stream-server"
