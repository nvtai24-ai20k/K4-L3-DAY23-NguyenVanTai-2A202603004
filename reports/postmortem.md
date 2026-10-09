# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

**Tóm tắt:** Region A bị netblock (SIGSTOP) lúc 02:33:48 UTC. Health checker phát hiện sau 17.9s,
runbook tự động (`--auto`) failover sang Region B, user nhận response OK đầu tiên từ B sau **29.2s**.
RPO tại thời điểm restore: **4.0s / 2 document**.

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T02:33:48 | outage bắt đầu (kill region a, netblock) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T02:33:53 | user đầu tiên bị ảnh hưởng (+4.9s, `ReadTimeout`) | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T02:34:06 | health check alert: region a `UNHEALTHY` (+17.9s) | `reports/health-events.jsonl:3` |
| 2026-10-09T02:34:10 | operator confirm (auto) + mở incident SEV1 (+21.3s, `notify_delay_s=21.29`) | `reports/runbook-run.jsonl:2` |
| 2026-10-09T02:34:10 | restore snapshot vào region b xong (+21.4s) | `reports/failover-events.jsonl:2` |
| 2026-10-09T02:34:16 | region b ready sau 6.42s warm-up; DNS cutover a → b (+27.9s) | `reports/failover-events.jsonl:5` |
| 2026-10-09T02:34:17 | golden signals ở b: 10/10 OK, p95 47.3ms | `reports/runbook-run.jsonl:6` |
| 2026-10-09T02:34:18 | resolved: request đầu tiên OK từ region b (+29.2s) | `reports/drill-2-withdr.jsonl:38` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: 29.2s · gap: −270.8s (còn dư 270.8s, PASS)
- RPO mục tiêu: 300s · đo được: 4.0s (2 doc bị mất) · gap: −296.0s (PASS)
- **Bước tốn nhiều giây nhất:** health-check detection — 17.9s / 29.2s = **61% RTO**.
  Vì `interval × threshold = 5s × 3 = 15s` là sàn cứng; cộng thêm 2.9s do kill rơi giữa 2 lần poll
  và mỗi probe vào region bị netblock phải chờ hết timeout 2s. Đứng thứ hai là GPU warm-up 6.4s (22%).

**Gap analysis — RPO 4.0s là con số lạc quan của môi trường lab.** Snapshot được restore
(`reports/replication.jsonl:2`) được `put` lúc 02:34:04 — **sau** t_outage 16s, vì trong lab
`state/ingest.py` và `state/replicate.py` ghi/đọc thẳng file SQLite của region A, không đi qua process bị SIGSTOP.
Trong một outage region thật, job replication cũng chết theo region; snapshot cuối cùng dùng được là
`reports/replication.jsonl:1` (02:33:31) → RPO thật ≈ **17.2s / 8 document** (đếm trực tiếp trong
`state/region-a/vectors.sqlite`: doc có `ingested_at` giữa snapshot đó và t_outage). Worst case với
`--every 30` và ingest 0.5 doc/s: ~30s / ~15 document. Vẫn nằm trong mục tiêu 300s.

## 3. Root cause (5 whys)

*Nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. Vì sao user lỗi 29s? — Vì chỉ có 1 region serve traffic (active-passive), region phụ ở trạng thái `warm`, rỗng.
2. Vì sao region phụ không serve được ngay? — Vì nó không có vector DB/weights và GPU pool chưa `full`; phải restore + warm-up 6.4s.
3. Vì sao phải đợi 17.9s mới bắt đầu restore? — Vì health checker cần 3 lần fail liên tiếp cách 5s để chống flap.
4. Vì sao runbook có thể thất bại trong outage thật? — `2_restore_snapshot` phụ thuộc snapshot do **chính region A** đẩy lên
   (`state/replicate.py` chạy cạnh primary). Nếu A chết trước lần `put` đầu tiên, hoặc object store đặt cùng region A,
   `snapshot.get` không có gì để restore → failover abort ở bước 2 (RTO = vô hạn).
   Ngoài ra `snapshot.rpo()` đọc DB của primary để tính `docs_lost` — trong outage thật đĩa của A không đọc được, nên RPO
   chỉ ước lượng được từ `MANIFEST.json` (`latest_doc_ts`), không đếm chính xác được doc bị mất.
5. Vì sao điều đó chưa được phát hiện trước? — Chưa có alert cho "replication lag" / "snapshot quá cũ"; drill chỉ chạy khi
   replication đã được khởi động bằng tay trước.

**Root cause hệ thống:** RPO và khả năng failover phụ thuộc vào một job replication chạy cùng fault domain với primary,
không có giám sát độ trễ replication.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Hạ health-check `interval` 5s → 2s (giữ `threshold=3`), probe 2 region song song | SRE on-call lead | 2026-10-16 | RTO −9s (floor 15s → 6s) |
| 2 | Kích hoạt `dr/runbook.py` trực tiếp từ alert UNHEALTHY (webhook) thay vì poll health log | Platform team | 2026-10-23 | RTO −3s |
| 3 | Pre-warm region phụ (`pool_state=full`) ngay khi health checker thấy fail đầu tiên của primary | ML infra owner | 2026-10-30 | RTO −6s (ẩn warm-up vào thời gian detect) |
| 4 | Replication cross-region chạy ở region phụ / object store khác region; alert khi `snapshot.py lag` > 60s | Data platform owner | 2026-10-30 | RPO worst case 30s → bị chặn & có cảnh báo; tránh abort ở bước 2 |
| 5 | Tăng tần suất replication 30s → 10s (hoặc CDC/WAL streaming cho vector DB) | Data platform owner | 2026-11-13 | RPO worst case −20s (~10 document) |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   5s × 3 = **15s**, chiếm 15 / 29.2 = **51% RTO**. Thời gian detect thực tế 17.9s (61% RTO) vì cộng thêm
   lệch pha poll và timeout 2s của mỗi probe khi region bị netblock.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   Floor còn 1 × 3 = 3s → RTO giảm khoảng **12s** (≈ 29.2s → ~17s), nhưng probe timeout 2s > interval 1s nên phải
   probe bất đồng bộ. Cái giá: 3 lần fail trong 3s rất dễ đến từ một GC pause, deploy rolling hay một blip mạng ngắn
   → false positive → failover không cần thiết, và nếu failback tự động thì 2 region flap qua lại (§4 Anti-Patterns).
   Probe traffic cũng tăng 5×. Muốn giảm RTO an toàn hơn: giảm interval vừa phải (2s) + giữ threshold, hoặc pre-warm
   region phụ song song với lúc đang detect.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   `docs_lost = 2` (thực tế ≈ 8 nếu tính theo snapshot trước outage) là số ticket khách hàng đã gửi thành công nhưng
   **biến mất vĩnh viễn**: RAG ở region B không bao giờ retrieve được chúng, câu trả lời về các ticket đó sẽ sai hoặc
   "không tìm thấy". Thời lượng outage 6h không làm `docs_lost` tăng (sau cutover, ingest mới ghi vào B), nhưng các doc đã
   mất không tự quay lại — cần thông báo cho khách hàng bị ảnh hưởng và yêu cầu gửi lại, hoặc replay từ nguồn gốc
   (queue/log ingest giữ lại ≥ 1 chu kỳ replication) nếu có.

## 6. Câu hỏi trong `dr/health_checker.py` — chọn interval cho RTO 5 phút

- **interval=5s, threshold=3 → sớm nhất phát hiện được outage sau bao lâu?** 5 × 3 = **15s** (sàn cứng).
  Trường hợp xấu nhất ≈ `interval × (threshold + 1) + timeout` = 5 × 4 + 2 = 22s (outage rơi ngay sau 1 lần poll OK,
  và mỗi probe vào region bị netblock phải chờ hết timeout). Drill này đo được 17.9s (`reports/health-events.jsonl:3`).
- **Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?** Các thành phần khác ngoài detection đo được là
  3.5 + 0.1 + 6.4 + 1.3 = 11.3s; nếu tính TTL xấu nhất 5s thay vì 1.3s thì khoảng 15s. Ngân sách còn lại cho detection:
  `4 × interval + 2 ≤ 300 − 15` → **interval ≤ ~70s** về lý thuyết. Nhưng con số này chỉ đúng với lab (restore 0.1s).
  Với model weights thật nhiều GB kéo từ S3 (restore + warm-up có thể mất vài phút), nên giữ detection ≤ ~60s →
  **interval ≤ 15s** với threshold=3. Lab này chọn 5s: detection chỉ chiếm 15s, còn dư hơn 270s cho restore thật.

## 7. Câu hỏi baseline (GUIDE Step 1 — trả lời trước khi viết code)

1. **Nếu Region A chết ngay bây giờ, thành phần nào phát hiện, bằng cách nào?** Không thành phần nào cả. Edge chỉ trả 503
   cho user, không ai poll `/readyz`, không có alert. Drill 1 chứng minh điều này: 15/15 request sau outage đều fail,
   `NO_RECOVERY` (`reports/drill-1-nodr.jsonl:32`).
2. **Region B có giữ dữ liệu hay model weights không?** Không: `count:0`, `weights:false`, `pool_state:warm`
   (`reports/failover-events.jsonl:1`, đo ngay trước khi restore).
3. **Nếu đổi `edge/active_region` sang `b` ngay lúc đó, user nhận được gì?** `region_not_ready` với lý do
   `pool_state=warm, model_weights_missing, vector_db_empty(count=0)` (`reports/health-events.jsonl:2`).
   Process còn sống không có nghĩa là region serve được inference.

## 8. Reflection (GUIDE)

1. **Thành phần nào giảm được mà không tăng rủi ro flapping, và cái giá là gì?** GPU warm-up (6.4s) và DNS TTL (1.3s, tối đa 5s).
   Không đụng tới logic phát hiện nên không gây flap. Warm-up: giữ region phụ ở `full` (hot standby), cái giá là trả tiền
   GPU idle 24/7, gần gấp đôi chi phí compute. Hoặc pre-warm khi thấy lần fail đầu tiên, rẻ hơn nhưng thỉnh thoảng
   warm-up thừa. TTL: hạ TTL, cái giá là edge/DNS resolver phải tra cứu nhiều hơn. Detection (17.9s) là phần lớn nhất,
   nhưng giảm nó chính là đánh đổi trực tiếp với flapping.
2. **Nếu health checker chạy chung process với serving API, ai báo động khi process đó chết?** Không ai. Health checker chết
   cùng lúc, nhất là với netblock/SIGSTOP: cả process bị đóng băng, không ghi được log nào. Vì vậy `dr/health_checker.py` chạy
   như một process riêng và chỉ import `argparse, json, pathlib, time, httpx`, **không import gì từ `serving/`**. Nó quan sát
   region từ bên ngoài qua HTTP, giống Route53 health check nằm ngoài region được kiểm tra.
3. **Khi ai đó hỏi "RTO 5 phút có thật không?", mở file nào?** `reports/drill-2-withdr.jsonl` (đồng hồ phía user, mỗi request
   1 dòng có timestamp) cùng `chaos/chaos-events.jsonl:3` (mốc 0), rồi chạy
   `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300`. Kết quả: `rto_measured_s=29.2`,
   lấy từ `reports/drill-2-withdr.jsonl:38` trừ t_outage, không phải ước lượng.
