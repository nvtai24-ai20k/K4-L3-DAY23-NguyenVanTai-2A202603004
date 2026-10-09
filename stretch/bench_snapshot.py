"""Stretch 1 — so sánh latency put/get của state/snapshot.py: backend fs vs minio.

Cần MinIO ở MINIO_URL (mặc định http://127.0.0.1:9000) và region-a đã seed.
get() GHI ĐÈ state/region-b -> chạy trong bản copy của repo, không chạy lúc đang drill.

    python3 stretch/bench_snapshot.py --n 10 --label weights-2mb --out stretch/results/minio/bench.jsonl
"""
import argparse
import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402


def ensure_bucket():
    c = snapshot._s3()
    if snapshot.BUCKET not in [b["Name"] for b in c.list_buckets().get("Buckets", [])]:
        c.create_bucket(Bucket=snapshot.BUCKET)


def timed(fn, n: int) -> list[float]:
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1000)
    return xs


def summary(xs: list[float]) -> dict:
    s = sorted(xs)
    return {"mean_ms": round(statistics.mean(s), 1), "p50_ms": round(statistics.median(s), 1),
            "p95_ms": round(s[max(0, -(-len(s) * 95 // 100) - 1)], 1),
            "min_ms": round(s[0], 1), "max_ms": round(s[-1], 1)}


def main(n: int, backends: list[str], label: str, out: pathlib.Path):
    src = pathlib.Path("state/region-a")
    size_mb = round(sum((src / f).stat().st_size for f in ("vectors.sqlite", "weights/model.bin"))
                    / 2**20, 2)
    if "minio" in backends:
        ensure_bucket()
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for b in backends:
        snapshot.put("a", b)  # warm-up: tạo bucket/thư mục, mở connection
        row = {"label": label, "backend": b, "n": n, "payload_mb": size_mb,
               "put": summary(timed(lambda: snapshot.put("a", b), n)),
               "get": summary(timed(lambda: snapshot.get("b", b), n))}
        rows.append(row)
        with out.open("a") as f:
            f.write(json.dumps(row) + "\n")
        print(json.dumps(row))
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--backends", nargs="+", default=["fs", "minio"])
    p.add_argument("--label", default="default")
    p.add_argument("--out", default="stretch/results/minio/bench.jsonl")
    a = p.parse_args()
    main(a.n, a.backends, a.label, pathlib.Path(a.out))
