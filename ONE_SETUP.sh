#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
if [ ! -x "${PYTHON_BIN}" ]; then
  PYTHON_BIN="python3"
fi

if [ -f ".venv/bin/activate" ]; then
  # shellcheck source=/dev/null
  source ".venv/bin/activate"
fi

EXPORT_DIR="${SCRIPT_DIR}/airtag-export"
ACCOUNT_JSON="${SCRIPT_DIR}/account.json"
DEVICE_JSON="${SCRIPT_DIR}/device.json"
ANI_LIBS="${SCRIPT_DIR}/ani_libs.bin"

echo "=========================================="
echo "FindMy One-Step Setup"
echo "=========================================="
echo

if [ ! -f "${ACCOUNT_JSON}" ]; then
  echo "No account.json found. Starting Apple login..."
  "${PYTHON_BIN}" - <<'PY'
from examples._login import get_account_sync

get_account_sync("account.json", None, "ani_libs.bin")
print("Saved account.json")
PY
else
  echo "Found existing account.json (login step skipped)"
fi

echo
echo "Exporting/decrypting local Find My accessories..."
mkdir -p "${EXPORT_DIR}"
"${PYTHON_BIN}" -m findmy decrypt --out-dir "${EXPORT_DIR}"

if ! compgen -G "${EXPORT_DIR}/*.json" > /dev/null; then
  echo
  echo "No accessory JSON files were exported to ${EXPORT_DIR}"
  echo "On Linux, decrypt usually needs access to Apple key material from a compatible environment."
  exit 1
fi

echo
echo "Selecting device JSON..."
"${PYTHON_BIN}" - <<'PY'
import glob
import json
import shutil
import sys
from pathlib import Path

export_dir = Path("airtag-export")
files = sorted(Path(p) for p in glob.glob(str(export_dir / "*.json")))
if not files:
    print("No exported JSON files found.")
    sys.exit(1)

records = []
for path in files:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    records.append(
        {
            "path": path,
            "name": data.get("name", "Unknown"),
            "model": data.get("model", "Unknown"),
            "paired_at": data.get("paired_at", "Unknown"),
            "has_keys": all(k in data for k in ("master_key", "skn", "sks")),
        }
    )

if len(records) == 1:
    chosen = records[0]
    print(f"Only one device found: {chosen['name']} ({chosen['model']})")
else:
    print("Exported devices:")
    for i, rec in enumerate(records, start=1):
        key_flag = "keys:ok" if rec["has_keys"] else "keys:missing"
        print(f"  {i}. {rec['name']} | {rec['model']} | {rec['paired_at']} | {key_flag}")
    while True:
        raw = input(f"Choose device [1-{len(records)}]: ").strip()
        try:
            idx = int(raw)
            if 1 <= idx <= len(records):
                chosen = records[idx - 1]
                break
        except ValueError:
            pass
        print("Invalid selection.")

shutil.copy2(chosen["path"], Path("device.json"))
print(f"Saved device.json from: {chosen['path'].name}")
if not chosen["has_keys"]:
    print("Warning: selected JSON does not contain complete key material.")
PY

echo
echo "Setup complete:"
echo "  - ${ACCOUNT_JSON}"
echo "  - ${DEVICE_JSON}"
echo "  - ${EXPORT_DIR}/*.json"
echo "  - ${ANI_LIBS} (created on first login)"
echo

read -r -p "Start web tracker now? [Y/n] " START_NOW
START_NOW="${START_NOW:-Y}"
if [[ "${START_NOW}" =~ ^[Yy]$ ]]; then
  exec "${SCRIPT_DIR}/RUN_WEB_TRACKER.sh"
fi

echo "You can start it later with: ./RUN_WEB_TRACKER.sh"
