"""Stretch 1 — chứng minh lỗi torn snapshot: put() gốc vs consistent_snapshot.put() khi đang ingest.

Ingest liên tục (thread, mặc định 50 doc/s) vào region-a trong lúc gọi put N lần mỗi backend.
Mỗi lần put xong: ghi nhận exception (BadDigest...) và chạy `PRAGMA integrity_check` trên bản
đã nằm trong object store (fs: đọc file; minio: download về) -> phát hiện bản copy rách.

Cần region-a đã seed và MinIO đang chạy. Ghi đè state/_replica và bucket -> chạy trong bản copy repo.
    python3 stretch/minio/torn_snapshot_test.py --n 30 --out stretch/results/minio/torn-snapshot.json
"""
import argparse
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402
from stretch.minio import consistent_snapshot  # noqa: E402


def ingest(stop: threading.Event, rate: float):
    con = sqlite3.connect("state/region-a/vectors.sqlite", timeout=5)
    i = 0
    while not stop.is_set():
        t = time.time()
        con.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)",
                    (f"torn-{int(t * 1e6)}", "x" * 2000, bytes([i % 256]) * 32, t))
        con.commit()
        i += 1
        time.sleep(max(0.0, 1 / rate - (time.time() - t)))
    con.close()


def stored_db_ok(backend: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        p = pathlib.Path(tmp) / "v.sqlite"
        if backend == "fs":
            p.write_bytes((snapshot._fs_root() / "vectors.sqlite").read_bytes())
        else:
            snapshot._s3().download_file(snapshot.BUCKET, "vectors.sqlite", str(p))
        try:
            con = sqlite3.connect(p)
            ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            con.close()
            return ok
        except sqlite3.DatabaseError:
            return False


def trial(put, backend: str, n: int) -> dict:
    errors, torn, err_types = 0, 0, {}
    for _ in range(n):
        try:
            put("a", backend)
        except Exception as e:
            errors += 1
            name = type(e).__name__ + (":BadDigest" if "BadDigest" in str(e) else "")
            err_types[name] = err_types.get(name, 0) + 1
            continue
        torn += not stored_db_ok(backend)
    return {"n": n, "put_errors": errors, "error_types": err_types, "torn_copies": torn}


def main(n: int, rate: float, out: pathlib.Path):
    c = snapshot._s3()
    if snapshot.BUCKET not in [b["Name"] for b in c.list_buckets().get("Buckets", [])]:
        c.create_bucket(Bucket=snapshot.BUCKET)
    stop = threading.Event()
    th = threading.Thread(target=ingest, args=(stop, rate), daemon=True)
    th.start()
    time.sleep(1)
    res = {"ingest_docs_per_s": rate, "db_mb": round(
        pathlib.Path("state/region-a/vectors.sqlite").stat().st_size / 2**20, 2)}
    try:
        for name, put in [("snapshot.put (goc)", snapshot.put),
                          ("consistent_snapshot.put", consistent_snapshot.put)]:
            for backend in ("fs", "minio"):
                r = trial(put, backend, n)
                res[f"{name} / {backend}"] = r
                print(name, backend, json.dumps(r))
    finally:
        stop.set()
        th.join()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--rate", type=float, default=50)
    p.add_argument("--out", default="stretch/results/minio/torn-snapshot.json")
    a = p.parse_args()
    main(a.n, a.rate, pathlib.Path(a.out))
