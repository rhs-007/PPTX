#!/bin/bash
# Convert every old deck in a folder (and its subfolders) without the web page.
# Double-click, then choose the folder. The converted decks go into the "output" folder here.

cd "$(dirname "$0")" || exit 1
FOLDER="$1"
if [ -z "$FOLDER" ]; then
  FOLDER="$(osascript -e 'POSIX path of (choose folder with prompt "Choose the folder of OLD decks to convert")' 2>/dev/null)"
fi
if [ -z "$FOLDER" ]; then
  exit 0
fi
bash ./Start.command batch "$FOLDER"
open "$(pwd)/output" 2>/dev/null
echo
read -n 1 -s -r -p "Done. Press any key to close this window."
echo
