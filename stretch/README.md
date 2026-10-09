# Stretch Goals — Lab 23

Cả 6 stretch goal trong [GUIDE.md](../GUIDE.md#stretch-goals). Không file nào của bài chính (`serving/`, `edge/`,
`state/`, `chaos/`, `tools/`, `dr/`, `reports/`) bị sửa. Mọi thứ nằm trong `stretch/`, kết quả trong `stretch/results/`.

> **Chạy ở đâu:** các script drill (`chaos_random.sh`, `drill_aa.sh`, `drill_minio.sh`, `torn_snapshot_test.py`)
> **xoá `state/` và `reports/*.jsonl`** mỗi lần chạy. Hãy chạy trong một bản copy của repo, để không ghi đè
> evidence đã nộp. Bare mode cần Linux (SIGSTOP), nên trên Windows thì chạy trong WSL:
>
> ```bash
> mkdir ~/lab23-copy && tar --exclude=.git --exclude='state/region-*' --exclude=state/_replica -cf - . | tar -xf - -C ~/lab23-copy
> cd ~/lab23-copy && find . \( -name '*.sh' -o -name '*.py' \) -exec sed -i 's/\r$//' {} +
> ```

| # | Stretch goal | Kết quả chính | Evidence |
|---|---|---|---|
| 1 | MinIO thật | put/get MinIO chậm hơn fs 7–23× (put) và 1.7–7× (get). Drill failover qua MinIO: **RTO 33.6s PASS**. Phát hiện bug **torn snapshot**: 12/30 lần `put` gốc bị `BadDigest` → đã fix, còn 0/30 | `stretch/results/minio/` |
| 2 | Postgres PITR | Restore về thời điểm ngay trước `DROP TABLE`: **215/215 doc, mất 0**, **RTO 2.9s** | `stretch/results/pitr/` |
| 3 | Active-active | Kill region A giữa lúc có traffic: **0 request lỗi**, p95 sau kill 1033ms. Conflict được giải quyết bằng LWW, 2 region hội tụ 265 = 265 doc | `stretch/results/active-active/` |
| 4 | Terraform | S3 CRR 2 region + versioning + RTC. `terraform validate` → valid | `stretch/terraform/main.tf` |
| 5 | Randomized chaos | 5 run: **RTO trung bình 25.12s, stdev 2.89s**. `stop` 22.25s và `netblock` 27.03s | `stretch/results/chaos-random/` |
| 6 | DR maturity | **Level 3** (thang tự định nghĩa ở dưới). Muốn lên Level 4 cần đưa replication ra khỏi fault domain của primary và chạy chaos định kỳ trong CI | mục 6 |

---

## 1. MinIO thật (`--backend minio`)

MinIO đã gỡ image `minio/minio` khỏi Docker Hub (`pull access denied`), và `quay.io/minio/minio` trả 401.
Nên `docker compose up minio` của repo không chạy được nữa. Thay bằng bản build của Chainguard, cùng cổng và
credentials như `docker-compose.yml`:

```bash
docker run -d --name lab23-minio --user 0 -p 9000:9000 -p 9001:9001 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  chainguard/minio:latest server /data --console-address :9001
python3 stretch/bench_snapshot.py --n 10 --label weights-2mb      # latency put/get fs vs minio
bash stretch/minio/drill_minio.sh                                 # drill 2 của GUIDE với --backend minio
python3 stretch/minio/torn_snapshot_test.py --n 30 --rate 50      # torn snapshot: put gốc vs put nhất quán
```

### 1a. Latency put/get (`stretch/results/minio/bench.jsonl`)

| Payload | Backend | put p50 | put p95 | get p50 | get p95 |
|---|---|---:|---:|---:|---:|
| 2 MB (200 doc + weights 2 MB), n=10 | fs | 3.7 ms | 4.1 ms | 3.4 ms | 3.9 ms |
| | minio | 84.1 ms | 90.0 ms | 25.2 ms | 36.8 ms |
| 200 MB (weights 200 MB), n=5 | fs | 283.2 ms | 314.0 ms | 294.4 ms | 338.3 ms |
| | minio | 2094.1 ms | 3416.5 ms | 509.6 ms | 577.6 ms |

- `put` qua MinIO chậm hơn fs **23× ở 2 MB** (phần lớn là overhead HTTP và MD5 cho mỗi object) và **7× ở 200 MB**
  (lúc này băng thông chiếm ưu thế). `get` chỉ chậm hơn 1.7× ở 200 MB.
- `get` mới là thứ nằm trong RTO (`2_restore_snapshot`), còn `put` nằm trong RPO (chu kỳ replicate). Với weights
  thật cỡ 5–20 GB, cứ đà ~2.5 s/GB thì restore mất hàng chục giây đến vài phút. Đó là lý do nên pre-stage weights
  ở region phụ, thay vì kéo về lúc failover.
- Lưu ý: benchmark chạy song song với drill randomized chaos (mục 5), nên con số có nhiễu do tranh CPU.

### 1b. Drill failover qua MinIO (`stretch/results/minio/drill/`)

`tools/measure_rto.py`: `valid:true`, `warnings:[]`, **RTO 33.6s PASS**, RPO 4.0s / 2 doc.

| Mốc (từ t_outage) | fs (bài chính) | MinIO | Evidence (MinIO) |
|---|---:|---:|---|
| Health check phát hiện | 17.9s | 15.0s | `stretch/results/minio/drill/health-events.jsonl` |
| Restore snapshot (`restore_s`) | 0.108s | 0.392s | `stretch/results/minio/drill/failover-events.jsonl:2` |
| DNS cutover | 27.9s | 28.2s | `stretch/results/minio/drill/failover-events.jsonl:5` |
| Request OK đầu tiên (RTO) | 29.2s | 33.6s | `stretch/results/minio/drill/drill-minio.jsonl` |

MinIO chỉ làm restore chậm thêm 0.28s. RTO cao hơn 4.4s chủ yếu do TTL cache của edge (5.4s so với 1.3s): cutover
rơi đúng lúc cache vừa được làm mới. Phần này là may rủi, không phải do backend.

### 1c. Bug tìm được: torn snapshot khi DB đang được ghi (`stretch/results/minio/torn-snapshot.json`)

Trong drill MinIO, `state/replicate.py` **crash** với lỗi
`S3UploadFailedError: BadDigest — The Content-Md5 you specified did not match what we received`.
Nguyên nhân: `snapshot.put` upload thẳng file `vectors.sqlite` trong lúc `state/ingest.py` đang ghi, nên file thay
đổi giữa lúc tính MD5 và lúc gửi. Hậu quả trong thực tế: **replication dừng im lặng, RPO tăng không giới hạn** và
không có alert nào. Lần này crash xảy ra sau failover nên drill không bị ảnh hưởng, nhưng đó chỉ là may mắn.

Fix (`stretch/minio/consistent_snapshot.py`, không sửa `state/snapshot.py`): chụp DB bằng **SQLite online backup
API** (nhất quán tại một thời điểm), đọc `latest_doc_ts` từ chính bản chụp, upload data trước rồi `MANIFEST.json`
sau cùng. Test 30 lần `put` mỗi loại, ingest 50 doc/s:

| | fs: lỗi put | fs: bản copy rách | minio: lỗi put | minio: bản copy rách |
|---|---:|---:|---:|---:|
| `state/snapshot.put` (gốc) | 0/30 | 0/30 | **12/30 BadDigest** | 0/30 |
| `consistent_snapshot.put` | 0/30 | 0/30 | **0/30** | 0/30 |

Với backend fs, 30 lần thử không bắt được bản copy rách nào (`PRAGMA integrity_check` đều ok). Nhưng `shutil.copy2`
không có cơ chế nào chống lỗi này: nó chỉ không kiểm tra, nên lỗi không lộ ra, chứ không có nghĩa là an toàn.

---

## 2. Postgres PITR (`stretch/pitr/pitr_drill.sh`)

```bash
bash stretch/pitr/pitr_drill.sh      # cần Docker; dùng postgres:16, tự dọn container + volume
```

Kịch bản: bảng `doc_meta` (metadata đi kèm vector DB) với WAL archiving (`archive_command` cp sang `/pitr/archive`)
→ `pg_basebackup` (200 doc) → ingest thêm 15 doc (chỉ có trong WAL) → ghi `T_target` → **sự cố: `DROP TABLE doc_meta`**
(lỗi vận hành, không phải mất region) → restore base backup + replay WAL tới `recovery_target_time = T_target`.

| Chỉ số | Giá trị | Evidence |
|---|---|---|
| Doc tại T_target | 215 (200 từ base backup + 15 chỉ có trong WAL) | `stretch/results/pitr/pitr-events.jsonl:3` |
| Doc sau restore | **215, mất 0** | `stretch/results/pitr/pitr-events.jsonl:8` |
| RTO lớp metadata | **2.9s** = copy base backup 1.03s + khởi động & replay 3 segment WAL 1.87s | `stretch/results/pitr/pitr-events.jsonl:8` |
| Postgres xác nhận | `recovery stopping before commit of transaction 749` (chính là lệnh DROP lúc 02:52:08.586) | `stretch/results/pitr/restore-postgres.log` |

So với snapshot của vector DB: snapshot định kỳ (`--every 30`) chỉ quay về được thời điểm của **snapshot gần nhất**
(RPO 2–24s trong 5 run ở mục 5). PITR quay về được **bất kỳ thời điểm nào** có WAL, nên RPO ≈ 0. Đặc biệt, PITR cứu
được cả lỗi logic như DROP hay DELETE nhầm. Với lỗi kiểu này, replication không giúp gì vì nó replicate luôn cả cái DROP.
RTO 2.9s ở đây là với 3 segment WAL. RTO của PITR tăng tuyến tính theo lượng WAL cần replay kể từ base backup, nên
cần lấy base backup định kỳ.

---

## 3. Active-active (`stretch/active_active/`)

```bash
bash stretch/active_active/drill_aa.sh
```

- **Edge** (`edge_aa.py`, cổng 8090, chạy song song và không thay `edge/proxy.py`): round-robin 50/50. Request lỗi
  (timeout 1s, refused, 503) thì **retry ngay sang region kia**. Có **circuit breaker**: sau 2 lần fail liên tiếp thì
  bỏ qua region đó 10s, hết cooldown thì thử lại đúng 1 request (half-open).
- **Cả 2 region đều `full`** và có data từ đầu (snapshot get trước khi boot), **cả 2 cùng nhận ingest**.
- **Conflict resolution** (`merge.py`, chạy mỗi 5s):
  - Doc chỉ có ở một bên (id có prefix region: `live-a-…`, `live-b-…`) thì copy sang bên kia. Ingest append-only
    với id duy nhất nên phép union là đủ. Đây **không phải conflict**.
  - Cùng `doc_id` nhưng nội dung khác là **conflict**, giải quyết bằng **Last-Writer-Wins theo `ingested_at`**; nếu
    hoà thì region `a` thắng. Tie-break cố định để cả 2 bên luôn chọn cùng một bản, đảm bảo hội tụ.
  - Không merge khi một region không trả lời `/healthz`, vì trong outage thật không đọc được đĩa của region chết.

| | Trước kill (31 req) | Sau kill region A (58 req) |
|---|---|---|
| Request lỗi | 0 | **0** |
| Region serve | a: 15, b: 16 | b: 58 |
| p50 / p95 / max latency | 18.9 / 30.4 / 1041.6 ms | 18.3 / **1033.0** / 1033.9 ms |
| Chuỗi fail dài nhất ("RTO" user thấy) | 0s | **0s** |

Evidence: `stretch/results/active-active/summary.json`, `stretch/results/active-active/loadgen-aa.jsonl`.

- 3 request chậm khoảng 1.03s sau kill (+1.5s, +16.4s, +27.9s) là cái giá của retry. Request đầu tiên trả hết
  timeout 1s cho A rồi mới sang B; hai request sau là các lần circuit half-open thử lại A sau mỗi cooldown 10s.
  Circuit của A mở (`fails:4, open:true`), xem `stretch/results/active-active/edge-state-after-kill.json`.
- **Conflict cố ý** `ticket-42`, sửa ở A rồi sửa ở B sau 1s: LWW chọn bản của B, và cả 2 region đều giữ
  `"khach doi dia chi + SDT (sua o region b, sau)"`.
- **Partition rồi hội tụ** (`stretch/results/active-active/merge.jsonl`): từ +4.9s đến +33.2s là `skip_partition`.
  Restore A lúc +35s; vòng merge ở +38.2s copy 18 doc mỗi chiều. Kết thúc: **265 = 265 doc, `converged:true`**
  (`stretch/results/active-active/convergence.json`).

**Đánh đổi:** RTO gần như 0 và không cần failover, nhưng phải trả **2× chi phí GPU** liên tục. Ngoài ra, LWW **âm thầm
bỏ** bản ghi thua: bản sửa ở A của `ticket-42` bị mất, và lệch đồng hồ giữa 2 region có thể chọn sai bản "mới hơn".
Data quan trọng cần vector clock hoặc CRDT, hoặc định tuyến mỗi khách hàng về một home region để tránh ghi đồng thời.
Bản thân `edge_aa.py` cũng là một single point of failure; ngoài thực tế lớp này là GSLB hoặc anycast.
Hạn chế của lab: trong lúc A bị SIGSTOP, `state/ingest.py` vẫn ghi thẳng vào đĩa của A, còn ngoài thực tế ingest
vào A sẽ fail theo.

---

## 4. Terraform write-only (`stretch/terraform/main.tf`)

Bucket `primary` (us-east-1) và `replica` (us-west-2), cả hai bật versioning và SSE. Kèm IAM role cho replication và
`aws_s3_bucket_replication_configuration` (không replicate delete marker, có RTC 15 phút cùng metrics để alert khi
lag tăng), cộng lifecycle giữ 30 ngày phiên bản cũ.

| Lab (`state/snapshot.py`) | AWS thật |
|---|---|
| `put(region="a")` | `PutObject` vào bucket primary ở region A |
| `state/replicate.py --every 30` + copy sang region B | Một phần RPO, cộng thêm độ trễ CRR (RTC: 99.99% < 15 phút) |
| `get(region="b")` | `GetObject` từ bucket **replica** ở region B, không bao giờ đọc từ primary đã chết |
| Ghi đè `MANIFEST.json` | Object versioning: mỗi lần put là một `VersionId`; rollback = `GetObject(VersionId=…)` |

**Bẫy:** CRR replicate từng object độc lập và không theo thứ tự, nên region B có thể thấy `MANIFEST.json` mới trước khi
`vectors.sqlite` mới tới. Vì vậy cần: upload data trước, MANIFEST sau cùng (như `consistent_snapshot.put`), ghi ETag
của data vào MANIFEST, và lúc `get` thì kiểm tra ETag.

Kiểm tra: `terraform fmt` không cần sửa gì; `terraform init -backend=false && terraform validate` →
`Success! The configuration is valid.` (provider `hashicorp/aws ~> 5.0`, **không apply**).

---

## 5. Randomized chaos (`stretch/chaos_random.sh`)

```bash
SEED=2310 RUNS=5 bash stretch/chaos_random.sh      # ~2 phút/run
```

Mỗi run làm lại từ đầu (seed, up, ingest, replicate), kill region A với **mode và thời điểm ngẫu nhiên** (8–25s sau
khi loadgen bắt đầu, có bảo đảm mỗi mode xuất hiện ít nhất 2 lần), rồi chạy `dr/runbook.py --auto` và
`tools/measure_rto.py`. Seed 2310 (`stretch/results/chaos-random/plan.txt`).

| run | mode | kill sau | lỗi đầu | phát hiện | cutover | **RTO** | RPO | doc mất | req lỗi | valid |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | stop | 8.3s | 0.3s | 11.7s | 18.5s | **21.8s** | 24.0s | 12 | 43 | ✓ |
| 2 | stop | 17.8s | 0.2s | 12.2s | 18.5s | **22.7s** | 4.0s | 2 | 45 | ✓ |
| 3 | netblock | 20.7s | 0.3s | 18.3s | 26.5s | **28.6s** | 14.0s | 7 | 13 | ✓ |
| 4 | netblock | 13.8s | 0.2s | 15.9s | 23.2s | **27.2s** | 2.0s | 1 | 12 | ✓ |
| 5 | netblock | 14.7s | 0.3s | 15.1s | 23.3s | **25.3s** | 4.0s | 2 | 11 | ✓ |

| Nhóm | n | mean RTO | stdev | min | max |
|---|---:|---:|---:|---:|---:|
| **Tất cả** | 5 | **25.12s** | **2.89s** | 21.8s | 28.6s |
| stop (SIGKILL) | 2 | 22.25s | 0.64s | 21.8s | 22.7s |
| netblock (SIGSTOP) | 3 | 27.03s | 1.66s | 25.3s | 28.6s |

Cả 5 run đều `valid:true` và `warnings:[]`. Cộng với drill chính (29.2s), 6 lần đo cho RTO trong khoảng 21.8–29.2s.

- **`stop` nhanh hơn `netblock` khoảng 5s**, và chênh lệch nằm hết ở khâu phát hiện (khoảng 12s so với 15–18s).
  Với SIGKILL, probe bị `ConnectError` ngay lập tức. Với netblock, mỗi probe phải chờ hết timeout 2s, và runbook
  cũng chờ timeout khi tự xác nhận outage. Fail chậm thì đắt hơn fail nhanh.
- **Phát hiện ở mode `stop` nhanh hơn "detect floor" 15s** mà `tools/measure_rto.py` giả định. Sàn thật ra là
  `interval × (threshold − 1)` = 10s cộng phần lệch pha: lần fail đầu tiên rơi ngay sau lúc kill, và 2 lần sau cách
  nhau 5s. Test `test_health_check_interval_duoc_ghi_lai` (yêu cầu ≥ floor − 1 = 14s) **sẽ đánh trượt một drill
  `stop` thật** dù không có log nào bị sửa tay. Đó là lý do bài chính chấm bằng `netblock`.
- **RPO dao động mạnh (2–24s)** dù RTO ổn định, vì nó phụ thuộc vào việc kill rơi vào đâu trong chu kỳ replicate 30s.
  Đúng như GUIDE nói: RPO sẽ biến động.
- Số request lỗi ở mode `stop` gấp khoảng 3.5 lần, không phải vì outage dài hơn mà vì request fail nhanh, nên loadgen
  gửi được nhiều request hơn trong cùng khoảng thời gian.

---

## 6. DR maturity self-assessment

> Repo không chứa định nghĩa thang Level 0–4 của slide. Thang dưới đây do mình tự định nghĩa theo cách phân cấp
> thông dụng. **Cần đối chiếu lại với slide** trước khi nộp.

| Level | Định nghĩa | Hệ thống này |
|---|---|---|
| 0 — Không có DR | Không backup, không runbook; region chết là mất trắng | Trạng thái ở drill 1: `NO_RECOVERY` |
| 1 — Có backup | Có snapshot/backup nhưng restore làm tay, chưa từng thử | ✓ |
| 2 — Runbook + đã thử | Replication định kỳ, runbook viết ra, failover tay đã thử ít nhất 1 lần | ✓ |
| 3 — Tự động phát hiện, failover bán tự động, có số đo | Health check chống flap, failover 1-click có điều kiện abort, RTO/RPO **đo từ log**, có drill lặp lại | **✓ Đang ở đây**: RTO 25.1 ± 2.9s qua 5 run ngẫu nhiên + drill chính; RPO đo bằng cả giây lẫn số doc |
| 4 — Liên tục & tự chữa | Active-active hoặc hot standby, RPO ≈ 0 (PITR/streaming), chaos chạy định kỳ tự động, replication được giám sát, failback có circuit breaker | Một phần: đã có prototype active-active, PITR và chaos script, nhưng chưa vận hành thật |

**Đang ở Level 3.** Những thay đổi cụ thể để lên Level 4, theo thứ tự ưu tiên:

1. **Đưa replication ra khỏi fault domain của primary và giám sát nó.** Đây là điểm yếu lớn nhất, đã thấy rõ ở
   mục 1c: `replicate.py` chạy cạnh primary và crash im lặng. Cần dùng `consistent_snapshot.put`, chạy replication ở
   region B hoặc S3 CRR (mục 4), và alert khi `snapshot.py lag` vượt 60s.
2. **Chạy `stretch/chaos_random.sh` định kỳ (ví dụ hằng tuần trong CI)** với gate `mean + 2σ < 300s` và `valid:true`.
   Hiện tại drill chỉ chạy khi có người nhớ.
3. **Hot standby hoặc active-active cho inference** (mục 3) để RTO gần 0, kèm PITR (mục 2) cho metadata, và failback
   có circuit breaker thay vì quyết định tay.
