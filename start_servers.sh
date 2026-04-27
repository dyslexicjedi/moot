#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start_servers.sh — Launch one llama.cpp server per council agent
#
# Usage:
#   chmod +x start_servers.sh
#   ./start_servers.sh
#
# Logs are written to logs/<agent>.log.
# Kill all servers: kill $(cat logs/*.pid)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── EDIT THESE ────────────────────────────────────────────────────────────────
# Path to llama-server binary (from llama.cpp build)
LLAMA_SERVER="${LLAMA_SERVER:-llama-server}"

# GPU layers — use -1 to offload everything to Metal (recommended for Apple Silicon)
NGL="-1"

# Context size per server (tokens). 4096 is safe; bump to 8192 if RAM allows.
CTX=4096

# Model paths — update these to your actual .gguf file locations
MODEL_BOB="/path/to/models/bob-70b-q4_k_m.gguf"        # chairman — best model you have
MODEL_GUPPY="/path/to/models/guppy-22b-q4_k_m.gguf"    # intel briefings — fast model
MODEL_RIKER="/path/to/models/riker-32b-q4_k_m.gguf"
MODEL_BILL="/path/to/models/bill-22b-q4_k_m.gguf"
MODEL_MILO="/path/to/models/milo-14b-q4_k_m.gguf"
# MODEL_HOMER="/path/to/models/homer-14b-q4_k_m.gguf"  # uncomment for 4th agent
# ─────────────────────────────────────────────────────────────────────────────

mkdir -p logs

start_server() {
    local name="$1"
    local port="$2"
    local model="$3"

    if [[ ! -f "$model" ]]; then
        echo "WARNING: Model not found for $name: $model — skipping."
        return
    fi

    echo "Starting $name on port $port using $(basename "$model") ..."
    "$LLAMA_SERVER" \
        --model "$model" \
        --host 127.0.0.1 \
        --port "$port" \
        --ctx-size "$CTX" \
        --n-gpu-layers "$NGL" \
        --parallel 1 \
        --log-disable \
        > "logs/${name}.log" 2>&1 &

    echo $! > "logs/${name}.pid"
    echo "  PID $(cat logs/${name}.pid) — logs//${name}.log"
}

start_server "Bob"   8001 "$MODEL_BOB"
start_server "Guppy" 8002 "$MODEL_GUPPY"
start_server "Riker" 8003 "$MODEL_RIKER"
start_server "Bill"  8004 "$MODEL_BILL"
start_server "Milo"  8005 "$MODEL_MILO"
# start_server "Homer" 8006 "$MODEL_HOMER"

echo ""
echo "All servers started. Waiting 10 s for models to load..."
sleep 10

echo ""
echo "Health check:"
for port in 8001 8002 8003 8004 8005; do
    if curl -sf "http://127.0.0.1:${port}/health" > /dev/null 2>&1; then
        echo "  Port $port — OK"
    else
        echo "  Port $port — not responding yet (may still be loading)"
    fi
done

echo ""
echo "To stop all servers: kill \$(cat logs/*.pid)"
