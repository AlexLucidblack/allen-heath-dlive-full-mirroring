#!/bin/bash
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.10+ is required / Требуется Python 3.10 или новее.'
  read -r
  exit 1
fi
python3 allen_heath_dlive_full_mirroring.py "$@"
result=$?
if [ "$result" -ne 0 ]; then read -r; fi
exit "$result"
