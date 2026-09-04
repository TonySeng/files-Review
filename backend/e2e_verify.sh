#!/usr/bin/env bash
PY="/c/Users/51575/.workbuddy/binaries/python/versions/3.13.12/python.exe"
BASE=http://localhost:9100
DOCX="d:/Private Documents/app-project/biddingfiles-Review/backend/uploads/3491dbb4c9514db28594c3728e8ca771.docx"
TMP=/tmp/e2e_test.docx
cp "$DOCX" "$TMP"

echo ">> upload"
UP=$(curl -s -F "files=@$TMP" "$BASE/api/files/upload")
FID=$("$PY" -c "import sys,json;print(json.load(sys.stdin)['files'][0]['file_id'])" <<< "$UP")
echo "file_id=$FID"

echo ">> fetch rule ids"
RULES=$(curl -s "$BASE/api/rulesets/rs-f97d16c3")
RIDS=$("$PY" -c '
import sys,json
d=json.load(sys.stdin)
def find(x):
    if isinstance(x,list):
        for r in x:
            if isinstance(r,dict) and r.get("id")=="rs-f97d16c3": return r
    if isinstance(x,dict):
        if x.get("id")=="rs-f97d16c3": return x
        for k in ("rulesets","items","data"):
            if k in x:
                r=find(x[k])
                if r: return r
    return None
r=find(d)
print(json.dumps([str(x.get("id")) for x in (r.get("rules",[]) if r else [])]))
' <<< "$RULES")
echo "rule_count=$("$PY" -c 'import sys,json;print(len(json.load(sys.stdin)))' <<< "$RIDS")"

echo ">> create task (117 rules, kb disabled to isolate LLM throughput)"
PAYLOAD=$("$PY" -c "import json,sys;print(json.dumps({'file_ids':[sys.argv[1]],'ruleset_id':'rs-f97d16c3','rule_ids':json.loads(sys.argv[2]),'kb_enabled':False}))" "$FID" "$RIDS")
TASK=$(curl -s -X POST "$BASE/api/review/tasks" -H "Content-Type: application/json" -d "$PAYLOAD")
TID=$("$PY" -c "import sys,json;print(json.load(sys.stdin).get('task_id'))" <<< "$TASK")
echo "task_id=$TID"

echo ">> poll"
START=$("$PY" -c "import time;print(int(time.time()))")
for i in $(seq 1 120); do
  ST=$(curl -s "$BASE/api/review/tasks/$TID")
  STATUS=$("$PY" -c "import sys,json;print(json.load(sys.stdin).get('status'))" <<< "$ST" 2>/dev/null)
  NOW=$("$PY" -c "import time;print(int(time.time()))")
  EL=$((NOW-START))
  echo "[${EL}s] status=$STATUS"
  if [ "$STATUS" = "done" ] || [ "$STATUS" = "failed" ] || [ "$STATUS" = "error" ]; then
    echo "$ST" | "$PY" -c "import sys,json;d=json.load(sys.stdin);print('  final_status=',d.get('status'));print('  findings=',len(d.get('findings',[])));print('  elapsed=',d.get('elapsed'))" 2>/dev/null
    break
  fi
  sleep 10
done
