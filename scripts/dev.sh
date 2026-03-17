#!/bin/bash
# Development script to run both backend and frontend

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

cleanup() {
  if [ -n "${BACKEND_PID:-}" ]; then
    kill "$BACKEND_PID" 2>/dev/null || true
  fi
  if [ -n "${FRONTEND_PID:-}" ]; then
    kill "$FRONTEND_PID" 2>/dev/null || true
  fi
}

# Start backend using pixi
echo "Starting backend..."
pixi run backend &
BACKEND_PID=$!

trap cleanup EXIT

echo "Waiting for backend readiness..."
BACKEND_READY=0
for _ in $(seq 1 120); do
  if curl -fsS "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
    BACKEND_READY=1
    break
  fi
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "Backend exited before becoming ready"
    wait "$BACKEND_PID" || true
    exit 1
  fi
  sleep 1
done

if [ "$BACKEND_READY" -ne 1 ]; then
  echo "Backend did not become ready within 120 seconds"
  exit 1
fi

# Start frontend only after the backend can answer requests.
echo "Starting frontend..."
cd "$PROJECT_ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "Backend running at http://127.0.0.1:8000"
echo "Frontend running at http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both servers"

wait
