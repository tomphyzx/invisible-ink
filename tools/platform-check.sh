#!/bin/bash
# Run every check on this computer and save the results in one file to send back.
#
#   tools/platform-check.sh [RESULTS_FILE]
#
# The first run sets up a private Python environment in .venv (needs python3 and the
# internet, once). Only synthetic test files are used; nothing of yours is read.

set -u
cd "$(dirname "$0")/.." || exit 2
OUT="${1:-$HOME/Desktop/invisible-ink-results.txt}"

if [ ! -x .venv/bin/python ]; then
  echo "Setting up Python for Invisible Ink (first run only)..."
  if ! python3 -m venv .venv || ! .venv/bin/python -m pip install --quiet --upgrade pip Pillow; then
    echo "Could not set up Python. Check that python3 is installed (brew install python)."
    exit 2
  fi
fi
PY=.venv/bin/python
TMP=$(mktemp -d)

echo "Running the checks. This takes a minute or two..."
{
  echo "== Invisible Ink platform check, $(date)"
  echo "== version: $(git rev-parse --short HEAD 2>/dev/null || echo 'downloaded copy, not a git checkout')"
  echo "== system: $(uname -a)"
  echo
  echo "== check"
  "$PY" bin/invisible-ink check
  echo
  echo "== tests"
  "$PY" -m unittest discover -s tests -v 2>&1
  echo
  echo "== samples"
  "$PY" tests/make_samples.py "$TMP/samples"
  for f in "$TMP"/samples/*; do
    echo
    echo "== scan $(basename "$f")"
    "$PY" bin/invisible-ink scan "$f"
    echo "exit code: $?"
  done
  echo
  echo "== clean"
  mkdir -p "$TMP/cleaned"
  "$PY" bin/invisible-ink clean -o "$TMP/cleaned" "$TMP"/samples/*
  echo "exit code: $?"
  for f in "$TMP"/cleaned/*; do
    echo
    echo "== rescan $(basename "$f")"
    "$PY" bin/invisible-ink scan "$f" | tail -2
  done
} > "$OUT" 2>&1
rm -rf "$TMP"

if grep -q "^OK" "$OUT"; then
  echo "All tests passed."
else
  echo "Some tests failed or were skipped; the results file has the details."
fi
echo "Results saved to: $OUT"
