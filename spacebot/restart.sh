#!/usr/bin/env bash
# Restart the Spacebot dev server (and Ollama, if it isn't up).
cd "$HOME/projects/SpaceLabs/spacebot" || exit 1

# Kill by listening port, not by command-line pattern — a pattern matches this script too.
PID=$(ss -lptn 'sport = :8080' 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)
[ -n "$PID" ] && kill "$PID" 2>/dev/null
sleep 1

if ! curl -s -m 2 http://127.0.0.1:11434/api/tags >/dev/null; then
  nohup setsid "$HOME/.local/bin/ollama" serve >"$HOME/ollama-serve.log" 2>&1 </dev/null &
  for _ in $(seq 1 30); do
    curl -s -m 1 http://127.0.0.1:11434/api/tags >/dev/null && break
    sleep 1
  done
fi

nohup setsid python3 -u server.py >"$HOME/spacebot.log" 2>&1 </dev/null &
for _ in $(seq 1 25); do
  curl -s -m 1 -o /dev/null http://127.0.0.1:8080/login && break
  sleep 0.4
done
sleep 0.5
cat "$HOME/spacebot.log"
