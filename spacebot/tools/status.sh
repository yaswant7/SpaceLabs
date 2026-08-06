#!/bin/bash
# What's running right now — the first thing to check when the app "won't start".
cd "$(dirname "$0")/.."
PORT="${SPACEBOT_PORT:-8080}"

pid=$(ss -lptnH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
if [ -n "$pid" ]; then
  echo "  spacebot   RUNNING on :$PORT  (pid $pid)  ->  http://localhost:$PORT"
else
  echo "  spacebot   not running on :$PORT"
fi

if curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  echo "  ollama     up at http://localhost:11434"
else
  echo "  ollama     DOWN — start it with:  ollama serve &"
fi

echo "  ollama bin $(command -v ollama || echo "$HOME/.local/bin/ollama")"
echo "  project    $(pwd)"
echo "  database   $(ls -lh data/spacebot.db 2>/dev/null | awk '{print $5, $9}' || echo 'missing')"
