# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong. Mọi lệnh chạy từ thư mục gốc repo.

**Trigger:** `reports/health-events.jsonl` có dòng `"event":"state_change","region":"a","to":"UNHEALTHY"`
(health checker: `interval=5s`, `threshold=3` → phát hiện sớm nhất sau ~15s).

**Đường tắt (khuyến nghị):** bước 1–7 đã được tự động hoá, mặc định hỏi `y/N` trước khi failover:

```bash
python3 dr/runbook.py --primary a --target b --backend fs      # bán tự động, hỏi confirm
```

Log của từng bước: `reports/runbook-run.jsonl` (7 bước) và `reports/failover-events.jsonl` (5 bước con).
Nếu script lỗi giữa chừng, làm tay theo bảng dưới.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` (chạy 3 lần, cách nhau ~5s) và `tail -n 5 reports/health-events.jsonl` | `a.ready=false` 3 lần liên tiếp **và** có alert `to:UNHEALTHY, region:a`; đồng thời `b.alive=true` (nếu b cũng chết → DỪNG, đây là double outage, escalate) | On-call (primary) |
| 2 | Mở incident + bấm giờ RTO | Mở kênh `#inc-region-a` SEV1; ghi mốc: `python3 -c "import time;print(time.time())"` | Có ts incident trong `reports/runbook-run.jsonl` (`step:2`) hoặc trong kênh incident | On-call (primary) → Incident Commander |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` rồi `python3 state/snapshot.py lag --backend fs` | Lệnh in `embed_model_version` + `restored_at`; `curl -s localhost:8002/v1/state` cho `weights:true`, `count>0`. Ghi lại `rpo_seconds` | On-call (primary) |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` rồi poll `curl -s -o /dev/null -w "%{http_code}\n" localhost:8002/readyz` mỗi 1s | `/readyz` của b trả **200** (sau ~6s warm-up). Quá 60s chưa 200 → **KHÔNG** sang bước 5, escalate | On-call (primary) |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl -s localhost:8080/edge/state` cho `active_region=b` (chờ tối đa TTL 5s); `curl -s localhost:8080/v1/infer` trả `"edge_region":"b"` | Incident Commander duyệt, on-call thực hiện |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w "%{http_code} %{time_total}\n" localhost:8002/v1/infer; done` | 10/10 trả `200`, p95 < **500ms**, error rate = **0%** | On-call (secondary) |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"valid":true`, `rto_verdict` = `PASS`; điền `reports/rto-evidence.md` + `reports/postmortem.md` trong 48h | Incident Commander |

**Rollback / abort trong lúc failover:**
- Bước 3 lỗi (không có snapshot / `MANIFEST.json` thiếu) hoặc bước 4 quá 60s → **không cutover**, `active_region` giữ `a`.
  `dr/failover.py` tự trả `pool_state` về giá trị cũ. Escalate lên Incident Commander.
- Sau cutover, golden signals ở bước 6 fail (error rate > 1% hoặc p95 > 500ms trong 5 phút) → không flip ngay
  về A (A vẫn đang chết); escalate, giữ B và điều tra.

**Failback (trả traffic về region A) — KHÔNG tự động:**
- Điều kiện (phải đủ cả 3):
  1. `reports/health-events.jsonl` có `region:a, to:HEALTHY` và A giữ HEALTHY liên tục ≥ **30 phút** (circuit breaker chống flap).
  2. Dữ liệu ghi vào B trong lúc incident đã được đồng bộ ngược về A (`python3 state/snapshot.py put --region b` → `get --region a`), `docs_lost` khi failback = 0.
  3. Thực hiện trong khung giờ thấp tải, có người trực.
- Ai quyết định: **Incident Commander** (không phải on-call một mình), sau khi Service Owner đồng ý.
- Cách làm: `python3 dr/failover.py --target a --backend fs` (cùng 5 bước, cùng điều kiện "không ready thì không cutover").
- Lý do không full-auto: §4 Anti-Patterns — failover 2 chiều không có circuit breaker làm 2 region flap qua lại.
