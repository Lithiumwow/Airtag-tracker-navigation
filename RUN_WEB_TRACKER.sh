#!/bin/bash

# FindMy Web Tracker Startup Script
# This script helps you start the web tracker application

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="python3"
fi

# Activate virtual environment if it exists
if [ -f ".venv/bin/activate" ]; then
    echo "Activating virtual environment..."
    source .venv/bin/activate
fi

echo "=========================================="
echo "FindMy Web Tracker"
echo "=========================================="
echo ""

# Check if device.json exists
if [ ! -f "device.json" ]; then
    echo "ERROR: device.json not found!"
    echo "Please ensure your AirTag/device JSON is saved as device.json"
    exit 1
fi

echo "✓ Found device.json"
echo ""

# Extract device name from device.json
DEVICE_NAME=$("${PYTHON_BIN}" -c "import json; data=json.load(open('device.json')); print(data.get('name', 'Unknown Device'))" 2>/dev/null || echo "Unknown Device")
echo "Device: $DEVICE_NAME"
echo ""

TRACKER_PORT=8008

# Keep the app on a stable URL. If an old tracker process is still bound, stop it.
EXISTING_PID=$(ss -ltnp "sport = :${TRACKER_PORT}" 2>/dev/null | awk -F'pid=' 'NR>1{split($2,a,","); print a[1]; exit}')
if [ -n "${EXISTING_PID}" ]; then
    EXISTING_CMDLINE=$(tr '\0' ' ' < "/proc/${EXISTING_PID}/cmdline" 2>/dev/null || true)
    if [[ "${EXISTING_CMDLINE}" == *"examples/web_tracker_app.py"* ]]; then
        echo "Port ${TRACKER_PORT} is in use by an older tracker instance (PID ${EXISTING_PID})."
        echo "Stopping old instance..."
        kill "${EXISTING_PID}" 2>/dev/null || true
        sleep 1
    else
        echo "ERROR: Port ${TRACKER_PORT} is busy (PID ${EXISTING_PID})."
        echo "Stop that process first, or change TRACKER_PORT in RUN_WEB_TRACKER.sh."
        exit 1
    fi
fi

# Start the web tracker
echo "Starting web tracker..."
echo ""
echo "The web tracker will be available at:"
echo "  - Web UI: http://localhost:${TRACKER_PORT}"
echo "  - API: http://localhost:${TRACKER_PORT}/api/location"
echo "  - Debug: http://localhost:${TRACKER_PORT}/api/location-history"
echo ""
echo "To access from another machine, replace 'localhost' with the server IP."
echo "Press Ctrl+C to stop the server"
echo ""
echo "=========================================="
echo ""

# Run the web tracker (venv Python so deps match interactive `source .venv/bin/activate`)
exec "${PYTHON_BIN}" "${SCRIPT_DIR}/examples/web_tracker_app.py" \
    --airtag-json "${SCRIPT_DIR}/device.json" \
    --host 0.0.0.0 \
    --port "${TRACKER_PORT}"
