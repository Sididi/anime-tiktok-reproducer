#!/bin/bash
# Development script to run both backend and frontend

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Start backend
echo "Starting backend..."
cd "$PROJECT_ROOT/backend"
source .venv/bin/activate
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

# Start frontend
echo "Starting frontend..."
cd "$PROJECT_ROOT/frontend"
npm run dev &
FRONTEND_PID=$!

# Trap to kill both on exit
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null" EXIT

echo ""
echo "Backend running at http://localhost:8000"
echo "Frontend running at http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both servers"

wait
