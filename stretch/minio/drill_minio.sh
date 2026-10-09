#!/usr/bin/env bash
# Stretch 1 — Drill 2 cua GUIDE (Step 4) nhung replicate + restore qua MinIO that (--backend minio).
#
# Can MinIO o http://127.0.0.1:9000 (minioadmin/minioadmin). Image minio/minio da bi go khoi
# Docker Hub; dung ban build cua Chainguard:
#   docker run -d --name lab23-minio --user 0 -p 9000:9000 -p 9001:9001 \
#     -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
#     chainguard/minio:latest server /data --console-address :9001
#
# CANH BAO: xoa state/ va reports/*.jsonl -> chay trong BAN COPY cua repo (xem stretch/README.md).
set -u
cd "$(dirname "$0")/../.."
OUT=${OUT:-stretch/results/minio/drill}
mkdir -p "$OUT" run reports
rm -f "$OUT"/*
tr -d '\r' < scripts/up_bare.sh > run/up_bare.sh
tr -d '\r' < scripts/down_bare.sh > run/down_bare.sh

bash run/down_bare.sh > /dev/null
rm -rf state/region-a state/region-b state/_replica run/*.pid run/*.log
rm -f reports/*.jsonl chaos/chaos-events.jsonl
python3 state/seed_vectors.py --region a --docs 200
python3 state/seed_vectors.py --region b --docs 0 --weights-mb 0
printf a > edge/active_region
python3 - <<'EOF'
import sys; sys.path.insert(0, ".")
from state import snapshot
c = snapshot._s3()
if snapshot.BUCKET not in [b["Name"] for b in c.list_buckets().get("Buckets", [])]:
    c.create_bucket(Bucket=snapshot.BUCKET)
# xoa snapshot cu de chac chan failover dung snapshot cua drill nay
for o in c.list_objects_v2(Bucket=snapshot.BUCKET).get("Contents", []):
    c.delete_object(Bucket=snapshot.BUCKET, Key=o["Key"])
print("bucket", snapshot.BUCKET, "ready")
EOF
bash run/up_bare.sh > /dev/null || { echo "stack khong len"; exit 1; }

python3 state/ingest.py --region a --rate 0.5 --duration 150 > /dev/null &
python3 state/replicate.py --every 30 --duration 150 --backend minio > /dev/null &
sleep 5
python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-minio.jsonl > /dev/null &
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 \
    --out reports/health-events.jsonl > /dev/null &
sleep 12
python3 chaos/kill_region.py --region a --mode netblock --mock
python3 dr/runbook.py --primary a --target b --backend minio --auto > /dev/null
wait
python3 tools/measure_rto.py --loadgen reports/drill-minio.jsonl --target-rto 300 | tee "$OUT/measure.json"
cp reports/drill-minio.jsonl reports/health-events.jsonl reports/failover-events.jsonl \
   reports/replication.jsonl reports/runbook-run.jsonl chaos/chaos-events.jsonl "$OUT/"
bash run/down_bare.sh > /dev/null
echo MINIO_DRILL_DONE
