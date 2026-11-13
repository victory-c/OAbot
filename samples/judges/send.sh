#!/bin/sh
# Send one judge's interview email to the running OAbot, same as pasting it in the dashboard.
#   sh samples/judges/send.sh voicebit-jay-patel          (server on port 8000)
#   sh samples/judges/send.sh voicebit-jay-patel 8001     (another port)
# Run with no argument to list the judges.
cd "$(dirname "$0")/../.." || exit 1
if [ -z "$1" ] || [ ! -f "samples/judges/$1.txt" ]; then
  echo "Usage: sh samples/judges/send.sh <judge> [port]"; echo "Judges:"
  for f in samples/judges/*.txt; do echo "  $(basename "$f" .txt)"; done
  exit 1
fi
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
"$PY" -c 'import sys,json,urllib.request as u
s,_,b=open("samples/judges/"+sys.argv[1]+".txt").read().partition("\n\n")
r=u.Request("http://localhost:"+sys.argv[2]+"/api/ingest",json.dumps({"subject":s.removeprefix("Subject: "),"body":b}).encode(),{"Content-Type":"application/json"})
print("Sent. OAbot item", json.loads(u.urlopen(r,timeout=10).read())["id"], "- watch the dashboard.")' "$1" "${2:-8000}"
