#!/usr/bin/env bash
# Stretch 3 — Drill active-active: ca 2 region full + cung ingest, edge 50/50 o cong 8090, kill region a.
#
# CANH BAO: xoa state/ va reports/*.jsonl -> chay trong BAN COPY cua repo (xem stretch/README.md).
#   bash stretch/active_active/drill_aa.sh
set -u
cd "$(dirname "$0")/../.."
OUT=${OUT:-stretch/results/active-active}
mkdir -p "$OUT" run reports
rm -f "$OUT"/*.jsonl "$OUT"/*.json
tr -d '\r' < scripts/up_bare.sh > run/up_bare.sh
tr -d '\r' < scripts/down_bare.sh > run/down_bare.sh

bash run/down_bare.sh > /dev/null
rm -rf state/region-a state/region-b state/_replica run/*.pid run/*.log
rm -f reports/*.jsonl chaos/chaos-events.jsonl
python3 state/seed_vectors.py --region a --docs 200
python3 state/seed_vectors.py --region b --docs 0 --weights-mb 0
# Active-active: region b KHONG con la standby rong — co du data + weights + pool full tu dau
python3 state/snapshot.py put --region a --backend fs > /dev/null
python3 state/snapshot.py get --region b --backend fs > /dev/null
printf full > state/region-b/pool_state
printf a > edge/active_region
bash run/up_bare.sh > /dev/null || { echo "stack khong len"; exit 1; }
python3 -m uvicorn stretch.active_active.edge_aa:app --host 127.0.0.1 --port 8090 \
  --log-level warning > run/edge-aa.log 2>&1 &
echo $! > run/edge-aa.pid
until curl -sf -o /dev/null localhost:8090/edge/state; do sleep 0.3; done
curl -s localhost:8090/edge/state; echo

# ingest dong thoi vao CA HAI region + anti-entropy moi 5s
python3 state/ingest.py --region a --rate 0.5 --duration 70 > /dev/null &
P1=$!
python3 state/ingest.py --region b --rate 0.5 --duration 70 > /dev/null &
P2=$!
python3 stretch/active_active/merge.py --every 5 --duration 85 --out "$OUT/merge.jsonl" > /dev/null &
P3=$!

# conflict co chu dich: cung 1 ticket bi sua o ca 2 region (b sua sau) -> LWW phai chon ban cua b
python3 - <<'EOF'
import sqlite3, time
t = time.time()
for region, body, ts in [("a", "ticket-42: khach doi dia chi (sua o region a)", t),
                         ("b", "ticket-42: khach doi dia chi + SDT (sua o region b, sau)", t + 1)]:
    con = sqlite3.connect(f"state/region-{region}/vectors.sqlite", timeout=5)
    con.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)", ("ticket-42", body, b"\x2a" * 32, ts))
    con.commit(); con.close()
print("injected conflict ticket-42")
EOF

python3 loadgen/traffic.py --url http://127.0.0.1:8090/v1/infer --duration 50 --rps 2 \
  --out "$OUT/loadgen-aa.jsonl" > /dev/null &
LG=$!
sleep 15
python3 chaos/kill_region.py --region a --mode netblock --mock
wait $LG
cp chaos/chaos-events.jsonl "$OUT/chaos-events.jsonl"
curl -s localhost:8090/edge/state > "$OUT/edge-state-after-kill.json"

# hoi phuc region a -> merge vong ke tiep phai lam 2 ben hoi tu
python3 chaos/kill_region.py restore --region a --backend bare
cp chaos/chaos-events.jsonl "$OUT/chaos-events.jsonl"
wait $P1 $P2 $P3  # KHONG `wait` tron: no se cho ca uvicorn edge-aa (chay mai mai)
python3 - "$OUT" <<'EOF'
import json, sqlite3, sys
def docs(r):
    con = sqlite3.connect(f"state/region-{r}/vectors.sqlite")
    d = dict(con.execute("SELECT doc_id, body FROM docs").fetchall()); con.close(); return d
a, b = docs("a"), docs("b")
res = {"count_a": len(a), "count_b": len(b), "converged": a == b,
       "only_in_a": len(a.keys() - b.keys()), "only_in_b": len(b.keys() - a.keys()),
       "ticket_42_a": a.get("ticket-42"), "ticket_42_b": b.get("ticket-42")}
json.dump(res, open(f"{sys.argv[1]}/convergence.json", "w"), indent=2, ensure_ascii=False)
print(json.dumps(res, ensure_ascii=False))
EOF
python3 stretch/active_active/analyze_aa.py "$OUT"
kill "$(cat run/edge-aa.pid)" 2> /dev/null
bash run/down_bare.sh > /dev/null
echo AA_DONE
