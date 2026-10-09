#!/usr/bin/env bash
# Stretch 5 — Randomized chaos: RUNS drill, moi drill random thoi diem kill + mode (stop|netblock).
#
# CANH BAO: script XOA state/ va reports/*.jsonl, chaos-events.jsonl moi vong lap.
# Chay trong mot BAN COPY cua repo, KHONG chay trong repo co evidence da nop:
#   rsync -a --exclude state/region-* --exclude state/_replica --exclude .git ./ ~/lab23-chaos/
#   cd ~/lab23-chaos && RUNS=5 bash stretch/chaos_random.sh
set -u
cd "$(dirname "$0")/.."
RUNS=${RUNS:-5}
SEED=${SEED:-$RANDOM}
OUT=${OUT:-stretch/results/chaos-random}
mkdir -p "$OUT" run reports
tr -d '\r' < scripts/up_bare.sh > run/up_bare.sh
tr -d '\r' < scripts/down_bare.sh > run/down_bare.sh

# Ke hoach: moi mode xuat hien it nhat 2 lan, thu tu + thoi diem kill (8..25s sau khi loadgen chay) random.
python3 - "$RUNS" "$SEED" > "$OUT/plan.txt" <<'EOF'
import random, sys
n, seed = int(sys.argv[1]), int(sys.argv[2])
rnd = random.Random(seed)
base = ["stop", "netblock"] * 2 if n >= 4 else []
modes = base + [rnd.choice(["stop", "netblock"]) for _ in range(n - len(base))]
rnd.shuffle(modes)
for m in modes:
    print(m, round(rnd.uniform(8, 25), 1))
EOF
echo "seed=$SEED"; cat "$OUT/plan.txt"

i=0
while read -r mode delay; do
  i=$((i + 1))
  echo "== run $i/$RUNS mode=$mode kill_after=${delay}s"
  bash run/down_bare.sh > /dev/null
  rm -rf state/region-a state/region-b state/_replica run/*.pid run/*.log
  rm -f reports/*.jsonl reports/*.json chaos/chaos-events.jsonl
  python3 state/seed_vectors.py --region a --docs 200 > /dev/null
  python3 state/seed_vectors.py --region b --docs 0 --weights-mb 0 > /dev/null
  printf a > edge/active_region
  bash run/up_bare.sh > /dev/null || { echo "stack khong len"; exit 1; }

  python3 state/ingest.py --region a --rate 0.5 --duration 120 > /dev/null &
  python3 state/replicate.py --every 30 --duration 120 --backend fs > /dev/null &
  sleep 5
  python3 loadgen/traffic.py --duration 90 --rps 2 --out reports/drill.jsonl > /dev/null &
  python3 dr/health_checker.py --interval 5 --threshold 3 --duration 90 \
      --out reports/health-events.jsonl > /dev/null &
  sleep "$delay"
  python3 chaos/kill_region.py --region a --mode "$mode" --mock > /dev/null
  python3 dr/runbook.py --primary a --target b --backend fs --auto > /dev/null
  wait
  python3 tools/measure_rto.py --loadgen reports/drill.jsonl --target-rto 300 > reports/measure.json

  d="$OUT/run-$i"
  mkdir -p "$d"
  cp reports/*.jsonl reports/measure.json chaos/chaos-events.jsonl "$d/"
  printf '{"run": %d, "mode": "%s", "kill_after_s": %s, "seed": %s}\n' "$i" "$mode" "$delay" "$SEED" > "$d/params.json"
  python3 -c "import json;m=json.load(open('$d/measure.json'));print('   rto', m['rto_measured_s'], 'valid', m['valid'], 'warn', m['warnings'], 'rpo', m['rpo_at_restore_s'], m['docs_lost'])"
done < "$OUT/plan.txt"

bash run/down_bare.sh > /dev/null
python3 stretch/summarize_chaos.py "$OUT"
