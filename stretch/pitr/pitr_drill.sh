#!/usr/bin/env bash
# Stretch 2 — Postgres PITR cho metadata store (§3 PITR).
#
# Kich ban: bang doc_meta (doc_id, embed_model_version, ingested_at) — metadata di kem vector DB.
#   1. primary bat WAL archiving (archive_command -> /pitr/archive)
#   2. pg_basebackup luc dau
#   3. ingest tiep 15 doc SAU base backup -> chi WAL moi giu duoc chung
#   4. ghi T_target, roi "su co": DROP TABLE doc_meta (loi van hanh, khong phai mat region)
#   5. restore base backup + replay WAL toi recovery_target_time = T_target, promote
#   6. do RTO cua lop nay (restore_start -> query dau tien OK) va kiem tra mat bao nhieu doc
#
# Can Docker. Chay tu thu muc goc repo:  bash stretch/pitr/pitr_drill.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=${OUT:-stretch/results/pitr}
IMG=${IMG:-postgres:16}
VOL=lab23-pitr
P=lab23-pg-primary
R=lab23-pg-restore
mkdir -p "$OUT"
LOG="$OUT/pitr-events.jsonl"
: > "$LOG"

ev() {  # ev '<json object>' -> them ts/iso, ghi 1 dong JSONL
  python3 -c 'import json,sys,time
d=json.loads(sys.argv[1]); print(json.dumps({"ts":time.time(),"iso":time.strftime("%Y-%m-%dT%H:%M:%S",time.gmtime()),**d}))' "$1" \
    | tee -a "$LOG"
}
q() { docker exec -i "$1" psql -U postgres -d lab -tAq -c "$2"; }
wait_up() {  # cho toi khi container nhan query that (khong phai server tam luc initdb)
  until docker exec "$1" psql -U postgres -d lab -tAc "select 1" > /dev/null 2>&1 \
        && docker logs "$1" 2>&1 | grep -q "$2"; do sleep 0.2; done
}
cleanup() { docker rm -f "$P" "$R" > /dev/null 2>&1 || true; docker volume rm "$VOL" > /dev/null 2>&1 || true; }

cleanup
docker volume create "$VOL" > /dev/null
docker run --rm -v "$VOL":/pitr "$IMG" bash -c \
  'mkdir -p /pitr/archive /pitr/base && chown -R postgres:postgres /pitr'

docker run -d --name "$P" -e POSTGRES_PASSWORD=lab -e POSTGRES_DB=lab -v "$VOL":/pitr "$IMG" \
  -c wal_level=replica -c archive_mode=on -c archive_timeout=10 \
  -c 'archive_command=test ! -f /pitr/archive/%f && cp %p /pitr/archive/%f' > /dev/null
wait_up "$P" "PostgreSQL init process complete"
ev '{"event":"primary_up","archive_mode":"on","archive_timeout_s":10}'

q "$P" "CREATE TABLE doc_meta(doc_id text PRIMARY KEY, embed_model_version text NOT NULL,
        ingested_at timestamptz NOT NULL DEFAULT clock_timestamp())"
q "$P" "INSERT INTO doc_meta(doc_id, embed_model_version)
        SELECT 'seed-' || g, 'vi-e5-base@v3' FROM generate_series(1, 200) g"

t=$(date +%s.%N)
docker exec -u postgres "$P" pg_basebackup -U postgres -D /pitr/base -Fp -Xs -c fast
ev "{\"event\":\"base_backup_done\",\"docs_in_backup\":$(q "$P" 'select count(*) from doc_meta'),\"took_s\":$(python3 -c "import time;print(round(time.time()-$t,2))")}"

for i in $(seq 1 15); do
  q "$P" "INSERT INTO doc_meta(doc_id, embed_model_version) VALUES ('live-$i', 'vi-e5-base@v3')"
  sleep 0.5
done
T_TARGET=$(q "$P" "select clock_timestamp()")
N_TARGET=$(q "$P" "select count(*) from doc_meta")
ev "{\"event\":\"recovery_target\",\"t_target\":\"$T_TARGET\",\"docs_at_target\":$N_TARGET,\"docs_after_base_backup\":15}"
sleep 1

q "$P" "DROP TABLE doc_meta"
ev '{"event":"disaster","what":"DROP TABLE doc_meta (loi van hanh)"}'
SEG=$(q "$P" "select pg_walfile_name(pg_switch_wal())")
until q "$P" "select last_archived_wal >= '$SEG' from pg_stat_archiver" | grep -q t; do sleep 0.2; done
ev "{\"event\":\"wal_archived\",\"last_segment\":\"$SEG\",\"archived_count\":$(q "$P" 'select archived_count from pg_stat_archiver')}"
docker stop "$P" > /dev/null
ev '{"event":"primary_stopped"}'

# ---------------- RESTORE (dong ho RTO cua lop metadata bat dau) ----------------
ev '{"event":"restore_start"}'
T0=$(date +%s.%N)
docker run --rm -v "$VOL":/pitr "$IMG" bash -c "
  cp -a /pitr/base /pitr/restore && touch /pitr/restore/recovery.signal
  cat >> /pitr/restore/postgresql.auto.conf <<EOF
restore_command = 'cp /pitr/archive/%f %p'
recovery_target_time = '$T_TARGET'
recovery_target_action = 'promote'
EOF
  chown -R postgres:postgres /pitr/restore && chmod 700 /pitr/restore"
T1=$(date +%s.%N)
docker run -d --name "$R" -e POSTGRES_PASSWORD=lab -e PGDATA=/pitr/restore -v "$VOL":/pitr "$IMG" \
  -c archive_mode=off > /dev/null
until [ "$(q "$R" 'select pg_is_in_recovery()' 2> /dev/null)" = "f" ]; do sleep 0.1; done
T2=$(date +%s.%N)
N_RESTORED=$(q "$R" "select count(*) from doc_meta")
LAST=$(q "$R" "select max(ingested_at) from doc_meta")
ev "{\"event\":\"restore_ready\",\"copy_base_s\":$(python3 -c "print(round($T1-$T0,2))"),\"start_and_replay_s\":$(python3 -c "print(round($T2-$T1,2))"),\"rto_s\":$(python3 -c "print(round($T2-$T0,2))"),\"docs_restored\":$N_RESTORED,\"docs_at_target\":$N_TARGET,\"docs_lost\":$((N_TARGET - N_RESTORED)),\"last_doc_ingested_at\":\"$LAST\"}"
docker logs "$R" 2>&1 | grep -E "starting point-in-time recovery|restored log file|recovery stopping|selected new timeline|ready to accept" \
  > "$OUT/restore-postgres.log" || true

cleanup
echo "PITR_DONE -> $LOG"
