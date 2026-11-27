#!/bin/bash
# PPT Template Migrator - double-click to start it on a Mac.
# The first run sets up a private Python environment in this folder (.venv); later runs start straight away.

cd "$(dirname "$0")" || exit 1
APP="$(pwd)"
VENV="$APP/.venv"

pause_and_exit() {
  echo
  read -n 1 -s -r -p "Press any key to close this window."
  echo
  exit "${1:-1}"
}

find_python() {
  for p in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null)" /usr/bin/python3; do
    [ -n "$p" ] && [ -x "$p" ] || continue
    # /usr/bin/python3 is only a placeholder until Apple's Command Line Tools are installed
    if [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then
      continue
    fi
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

if [ ! -x "$VENV/bin/python3" ] || ! "$VENV/bin/python3" -c "import lxml, PIL" >/dev/null 2>&1; then
  PY="$(find_python)"
  if [ -z "$PY" ]; then
    echo "Python 3 is not installed on this Mac."
    echo
    echo "1. Install it from https://www.python.org/downloads/macos/"
    echo "   (opening that page now - choose the latest 'macOS 64-bit universal2 installer')."
    echo "2. Then double-click Start.command again."
    open "https://www.python.org/downloads/macos/" 2>/dev/null
    pause_and_exit 1
  fi
  echo "First run: setting things up with $("$PY" --version 2>&1). This takes about a minute ..."
  rm -rf "$APP/.venv"
  if ! "$PY" -m venv "$VENV"; then
    echo "Could not create the Python environment in $VENV"
    pause_and_exit 1
  fi
  "$VENV/bin/python3" -m pip install --quiet --upgrade pip >/dev/null 2>&1
  if ! "$VENV/bin/python3" -m pip install --quiet -r "$APP/requirements.txt"; then
    echo
    echo "Could not download the two helper packages (lxml, Pillow)."
    echo "Check the internet connection, then double-click Start.command again."
    pause_and_exit 1
  fi
  echo "Setup done."
  echo
fi

"$VENV/bin/python3" -m ppt_migrator "$@"
status=$?
if [ "$status" -ne 0 ]; then
  pause_and_exit "$status"
fi
exit 0
