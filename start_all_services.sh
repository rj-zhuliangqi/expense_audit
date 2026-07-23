#!/usr/bin/env bash
set -euo pipefail

export PYTHONUNBUFFERED=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/.venv/bin/python"
ENV_FILE="$ROOT_DIR/.env"
LOG_DIR="$ROOT_DIR/output/service-logs"
PID_DIR="$ROOT_DIR/output/service-pids"

GRAPH_PORT="18090"
NODE_PORT="8091"

GRAPH_LOG="$LOG_DIR/graph_runtime.log"
NODE_LOG="$LOG_DIR/node_gateway.log"
WORKER_LOG="$LOG_DIR/rabbitmq_worker.log"

GRAPH_PID_FILE="$PID_DIR/graph_runtime.pid"
NODE_PID_FILE="$PID_DIR/node_gateway.pid"
WORKER_PID_FILE="$PID_DIR/rabbitmq_worker.pid"

mkdir -p "$LOG_DIR" "$PID_DIR"

# Parse command argument: stop | start | restart (default)
ACTION="${1:-restart}"
case "$ACTION" in
  stop|start|restart) ;;
  *)
    echo "[error] Unknown action: $ACTION"
    echo "[usage] $0 {stop|start|restart}"
    exit 1
    ;;
esac

if [[ ! -x "$VENV_PY" ]]; then
  echo "[error] Missing python runtime: $VENV_PY"
  echo "[hint] Run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[error] Missing env file: $ENV_FILE"
  exit 1
fi

# Load .env variables for worker startup.
set -a
source "$ENV_FILE"
set +a

if [[ -z "${RABBITMQ_URL:-}" ]]; then
  echo "[error] RABBITMQ_URL is empty after loading .env"
  exit 1
fi

stop_pidfile_proc() {
  local name="$1"
  local pid_file="$2"
  if [[ ! -f "$pid_file" ]]; then
    return
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "[stop] $name pid=$pid"
    kill "$pid" 2>/dev/null || true
    for _ in {1..10}; do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "[stop] $name pid=$pid still running, sending SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi

  rm -f "$pid_file"
}

kill_by_port() {
  local port="$1"
  local pids=""

  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
  elif command -v fuser >/dev/null 2>&1; then
    pids="$(fuser -n tcp "$port" 2>/dev/null || true)"
  fi

  if [[ -z "$pids" ]]; then
    return
  fi

  echo "[stop] port $port occupied by pid(s): $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null || true
}

kill_by_pattern() {
  local pattern="$1"
  local pids
  pids="$(pgrep -f "$pattern" || true)"
  if [[ -z "$pids" ]]; then
    return
  fi

  echo "[stop] pattern '$pattern' pid(s): $pids"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  # shellcheck disable=SC2086
  kill -9 $pids 2>/dev/null || true
}

wait_for_health() {
  local url="$1"
  local name="$2"
  for _ in {1..30}; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "[ok] $name is healthy: $url"
      return 0
    fi
    sleep 1
  done
  echo "[error] $name did not become healthy in time: $url"
  return 1
}

start_bg() {
  local name="$1"
  local log_file="$2"
  local pid_file="$3"
  shift 3

  echo "[start] $name"
  nohup "$@" >>"$log_file" 2>&1 &
  local pid=$!
  echo "$pid" >"$pid_file"
  echo "[start] $name pid=$pid log=$log_file"
}

cleanup_old_logs() {
  echo "[step] cleaning historical logs in $LOG_DIR"
  find "$LOG_DIR" -maxdepth 1 -type f -name "*.log*" -delete
}

echo "[step] stopping old services"
stop_pidfile_proc "graph_runtime" "$GRAPH_PID_FILE"
stop_pidfile_proc "node_gateway" "$NODE_PID_FILE"
stop_pidfile_proc "rabbitmq_worker" "$WORKER_PID_FILE"

kill_by_port "$GRAPH_PORT"
kill_by_port "$NODE_PORT"
kill_by_pattern "uvicorn graph_runtime.api:create_app"
kill_by_pattern "uvicorn node_gateway.api:create_app"
kill_by_pattern "rabbitmq_worker.py"

if [[ "$ACTION" == "stop" ]]; then
  echo "[done] services stopped"
  exit 0
fi

cleanup_old_logs

echo "[step] starting graph_runtime on :$GRAPH_PORT"
start_bg \
  "graph_runtime" \
  "$GRAPH_LOG" \
  "$GRAPH_PID_FILE" \
  "$VENV_PY" -u -m uvicorn graph_runtime.api:create_app --factory --host 127.0.0.1 --port "$GRAPH_PORT"

wait_for_health "http://127.0.0.1:$GRAPH_PORT/health" "graph_runtime"

echo "[step] starting node_gateway on :$NODE_PORT"
start_bg \
  "node_gateway" \
  "$NODE_LOG" \
  "$NODE_PID_FILE" \
  "$VENV_PY" -u -m uvicorn node_gateway.api:create_app --factory --host 127.0.0.1 --port "$NODE_PORT"

wait_for_health "http://127.0.0.1:$NODE_PORT/health" "node_gateway"

echo "[step] starting rabbitmq_worker"
start_bg \
  "rabbitmq_worker" \
  "$WORKER_LOG" \
  "$WORKER_PID_FILE" \
  env GRAPH_RUNTIME_URL="http://127.0.0.1:$GRAPH_PORT" "$VENV_PY" -u rabbitmq_worker.py \
    --amqp-url "$RABBITMQ_URL" \
    --audit-service-url "https://service-uate-gw.ruijie.com.cn" \
    --ei-code-map-path "expense_audit_orchestrator/profiles/ei_code_map.json" \
    --queues "audit" \
    --prepared-output-dir "output/worker-debug/prepared" \
    --writeback-output-dir "output/worker-debug/writeback"

echo "[done] all services started"
echo "[logs] graph_runtime:   $GRAPH_LOG"
echo "[logs] node_gateway:    $NODE_LOG"
echo "[logs] rabbitmq_worker: $WORKER_LOG"
echo "[tail] tail -f $WORKER_LOG"