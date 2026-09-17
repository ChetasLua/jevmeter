#!/usr/bin/env bash
# One-command install for macOS and Linux:  ./install.sh
# Creates .venv/ in this folder, installs jevmeter with the right speech-to-text backend, then asks for your key.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9+ is required: https://www.python.org/downloads/" >&2
  exit 1
fi

echo "==> creating virtual environment with $PY"
"$PY" -m venv .venv
. .venv/bin/activate
python -m pip install --quiet --upgrade pip

if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ]; then EXTRA="mlx"; else EXTRA="cpu"; fi
echo "==> installing jevmeter[$EXTRA] (this can take a few minutes the first time)"
python -m pip install --quiet -e ".[$EXTRA]"

echo
echo "==> checking your setup"
jevmeter doctor || true
if ! python -c "from jevmeter.config import get_key; import sys; sys.exit(0 if get_key() else 1)"; then
  jevmeter setup
fi

cat <<'MSG'

  Done! To make a video:

    source .venv/bin/activate
    jevmeter

  Then drag your video into the terminal and answer 3 quick questions.
MSG
