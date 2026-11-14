#!/bin/sh
# Build-time preparation. The probe (agent-reach channels) must work or the build stops; each `agent-reach add <channel>` is recorded, never hidden, and does not stop the build
# (a channel can need a login that only exists at run time).
set -u
out=/app/bootstrap.json
agent-reach channels >/dev/null 2>&1 || { echo "agent-reach is not working in this image" >&2; exit 1; }
printf '{"agent_reach_ref":"%s","steps":{' "${AGENT_REACH_REF:-unknown}" > "$out"
first=1
for channel in instagram reddit twitter facebook linkedin xiaohongshu boss; do
  if agent-reach add "$channel" >/tmp/add-"$channel".log 2>&1; then result=ok; else result=failed; fi
  [ "$first" = 1 ] || printf ',' >> "$out"
  printf '"%s":"%s"' "$channel" "$result" >> "$out"
  first=0
done
printf '}}\n' >> "$out"
