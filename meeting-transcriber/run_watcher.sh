#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f "$HOME/.meeting-transcriber.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$HOME/.meeting-transcriber.env"
  set +a
fi

exec /usr/bin/python3 "$PWD/meeting_transcriber.py" --config "$PWD/config.json"
