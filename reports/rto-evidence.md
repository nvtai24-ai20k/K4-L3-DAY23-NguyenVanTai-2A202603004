# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.

Môi trường chạy: bare mode (`scripts/up_bare.sh`) trong WSL2 Ubuntu, chaos `--mode netblock --mock`
(SIGSTOP), health checker `interval=5s, threshold=3, timeout=2s`, edge TTL 5s, `WARMUP_SECONDS=6`.
Kết quả `tools/measure_rto.py` của từng drill được lưu ở reports/measure-drill-1.json và reports/measure-drill-2.json.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T02:32:56` | chaos kill region a (netblock) | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+2.0s` | dòng `ok:false` đầu tiên sau t_outage (`ReadTimeout`, 2027.5ms) | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | 15/15 request sau t_outage đều fail, tới hết file (dòng cuối +30.8s) | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py`: `requests_failed=15`, `rto_measured_s=null` | `reports/drill-1-nodr.jsonl:32` |

## 2. Drill 2 — có DR

t_outage = `2026-10-09T02:33:48` (ts `1791513228.844`).

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +4.9s | dòng `ok:false` đầu (`ReadTimeout`, edge vẫn trỏ `a`) | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | +17.9s | `to:UNHEALTHY, region:a`, `consecutive_fails:3` | `reports/health-events.jsonl:3` |
| Snapshot restore xong | +21.4s | `step:2_restore_snapshot` (`restore_s=0.108`) | `reports/failover-events.jsonl:2` |
| Region phụ ready | +27.9s | `step:4_wait_ready` (`waited_s=6.42`, 216 vectors) | `reports/failover-events.jsonl:4` |
| DNS cutover | +27.9s | `step:5_dns_cutover` (`a → b`) | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+29.2s** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:38` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 29.2s | 300s (5 phút) | **PASS** (`valid:true`, `warnings:[]`) |
| RPO — Vector DB | 4.0s / 2 doc | 300s (5 phút) | **PASS** — `reports/failover-events.jsonl:2` |

## 3. RTO của tôi gồm những gì

Tổng các dòng dưới = 17.9 + 3.5 + 0.1 + 6.4 + 1.3 = **29.2s** = `rto_measured_s`.

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 17.9s (floor 15.0s + 2.9s lệch pha poll/timeout) | `interval_s × threshold` = 5 × 3 trong `reports/health-events.jsonl:3` | Giảm `interval` (vd 2s × 3 = 6s floor), giữ `threshold=3` để vẫn chống flap; probe song song 2 region |
| Runbook xác nhận outage + mở incident | 3.5s | alert (`reports/health-events.jsonl:3`) → `1_verify_target` (`reports/failover-events.jsonl:1`): runbook tự probe 3 lần + đọc alert | Kích hoạt runbook trực tiếp từ alert (webhook) thay vì poll; probe cách 1s thay vì 2s |
| Snapshot restore | 0.1s | `2_restore` → `3_scale` (`reports/failover-events.jsonl:2`–`reports/failover-events.jsonl:3`) | Đã nhỏ với fs backend; với S3/MinIO thật: pre-stage snapshot sẵn ở region phụ (warm standby) |
| GPU pool warm-up | 6.4s | `waited_s` ở `4_wait_ready` (`reports/failover-events.jsonl:4`) | Giữ region phụ ở `full` (hot standby, tốn tiền GPU) hoặc pre-warm khi health check vừa thấy fail đầu tiên |
| DNS/LB TTL cache | 1.3s | t_recovered − t_cutover (`reports/drill-2-withdr.jsonl:38` − `reports/failover-events.jsonl:5`) | Hạ TTL edge (5s là trần); lần này cache tình cờ hết hạn sớm |
