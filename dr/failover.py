"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """/v1/state của 1 region; không raise — region không trả lời thì trả về error."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def _abort(result: dict, step: str, why: str, **kw) -> dict:
    emit(step=step, ok=False, aborted=True, reason=why, **kw)
    result.update(ok=False, aborted_at=step, reason=why)
    return result


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Trả về dict có ok + kết quả từng bước."""
    primary = "a" if target == "b" else "b"
    tdir = pathlib.Path(f"state/region-{target}")
    t_start = time.time()
    result = {"target": target, "primary": primary, "backend": backend, "ok": False, "steps": {}}

    # 1 — region phụ có sống không, và đang thiếu gì (weights? vectors? pool?)
    st = state_of(target)
    result["steps"]["1_verify_target"] = st
    if "error" in st:
        return _abort(result, "1_verify_target", f"region-{target} khong phan hoi: {st['error']}",
                      target=target)
    emit(step="1_verify_target", ok=True, target=target, pool_state=st.get("pool_state"),
         weights=st.get("weights"), vector_count=st.get("count"))

    # 2 — restore snapshot + đo RPO thật (primary có mà bản restore không có)
    t = time.time()
    try:
        meta = snapshot.get(target, backend)
    except (Exception, SystemExit) as e:
        return _abort(result, "2_restore_snapshot", f"restore that bai: {e}", target=target)
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     tdir / "vectors.sqlite")
    snap_at = meta.get("snapshot_at")
    restore = {"rpo_seconds": r["rpo_seconds"], "docs_lost": r["docs_lost"],
               "embed_model_version": meta.get("embed_model_version"),
               "snapshot_at": snap_at,
               "snapshot_age_s": None if snap_at is None else round(time.time() - snap_at, 2),
               "primary_latest_doc_ts": r["primary_latest_doc_ts"],
               "restored_latest_doc_ts": r["restored_latest_doc_ts"],
               "restore_s": round(time.time() - t, 3)}
    result["steps"]["2_restore_snapshot"] = restore
    emit(step="2_restore_snapshot", ok=True, target=target, backend=backend, **restore)

    # 3 — warm -> full. serving/app.py bắt đầu đếm warm-up từ lần đọc pool_state kế tiếp.
    pool = tdir / "pool_state"
    prev_pool = pool.read_text().strip() if pool.exists() else None
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text("full")
    result["steps"]["3_scale_pool"] = {"from": prev_pool, "to": "full"}
    emit(step="3_scale_pool", ok=True, target=target, pool_from=prev_pool, pool_to="full")

    # 4 — chờ /readyz 200. Timeout -> ABORT, không cutover (DNS trỏ vào region chưa ready
    #     = 503 từ cả hai phía).
    t, last = time.time(), "never_polled"
    while True:
        try:
            resp = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            body = resp.json()
            if resp.status_code == 200:
                break
            last = ",".join(body.get("reasons") or []) or f"http_{resp.status_code}"
        except Exception as e:
            last = type(e).__name__
        if time.time() - t >= wait:
            # trả pool về như cũ: không giữ GPU pool "full" cho region không serve được
            if prev_pool is None:
                pool.unlink(missing_ok=True)
            else:
                pool.write_text(prev_pool)
            return _abort(result, "4_wait_ready", f"target khong ready sau {wait}s: {last}",
                          target=target, waited_s=round(time.time() - t, 2),
                          pool_rolled_back_to=prev_pool)
        time.sleep(0.5)
    # /readyz 200 => không có "model_weights_missing" => weights đã có trên đĩa
    ready = {"waited_s": round(time.time() - t, 2), "weights": True,
             "vector_count": body["vectors"]["count"],
             "latest_doc_ts": body["vectors"]["latest_doc_ts"], "pool_state": body["pool_state"]}
    result["steps"]["4_wait_ready"] = ready
    emit(step="4_wait_ready", ok=True, target=target, **ready)

    # 5 — chỉ bây giờ mới đổi "DNS". Ghi file tạm rồi replace để edge không đọc file dở.
    prev_active = ACTIVE.read_text().strip() if ACTIVE.exists() else None
    tmp = ACTIVE.with_suffix(".tmp")
    tmp.write_text(target)
    tmp.replace(ACTIVE)
    result["steps"]["5_dns_cutover"] = {"from": prev_active, "to": target}
    emit(step="5_dns_cutover", ok=True, active_from=prev_active, active_to=target,
         total_s=round(time.time() - t_start, 2))
    result.update(ok=True, total_s=round(time.time() - t_start, 2))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
