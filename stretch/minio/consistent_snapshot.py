"""Stretch 1 — put() nhất quán cho vector DB đang được ghi (fix cho lỗi phát hiện khi chạy MinIO).

state/snapshot.py put() copy/upload thẳng file vectors.sqlite trong lúc state/ingest.py đang ghi:
  * --backend minio: file đổi giữa chừng -> MD5 không khớp -> BadDigest -> state/replicate.py
    CRASH, replication dừng im lặng, RPO tăng không giới hạn.
  * --backend fs: shutil.copy2 không kiểm tra gì -> có thể ra bản copy RÁCH (torn) mà không ai biết.
  * latest_doc_ts trong MANIFEST được đọc TRƯỚC khi copy -> có thể lệch với data thật.

Bản này: chụp DB bằng SQLite online backup API (snapshot nhất quán tại 1 thời điểm), đọc
latest_doc_ts TỪ BẢN CHỤP, upload data trước và MANIFEST SAU CÙNG (§ bẫy CRR trong stretch/terraform).
Không sửa state/snapshot.py — get()/lag()/rpo() của nó dùng lại được nguyên vẹn.
"""
import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402


def consistent_copy(src_db: pathlib.Path, dst_db: pathlib.Path):
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True, timeout=5)
    dst = sqlite3.connect(dst_db)
    try:
        src.backup(dst)  # một transaction đọc -> không bao giờ thấy ghi dở
    finally:
        dst.close()
        src.close()


def put(region: str, backend: str) -> dict:
    src = pathlib.Path(f"state/region-{region}")
    with tempfile.TemporaryDirectory() as tmp:
        db = pathlib.Path(tmp) / "vectors.sqlite"
        consistent_copy(src / "vectors.sqlite", db)
        meta = {"snapshot_at": time.time(), "source_region": region,
                "latest_doc_ts": snapshot.latest_doc_ts(db),
                "embed_model_version": (src / "weights" / "VERSION").read_text().strip(),
                "consistent": True}
        if backend == "fs":
            dst = snapshot._fs_root()
            shutil.copy2(db, dst / "vectors.sqlite")
            shutil.copy2(src / "weights" / "model.bin", dst / "model.bin")
            (dst / "MANIFEST.json").write_text(json.dumps(meta, indent=2))
        else:
            c = snapshot._s3()
            c.upload_file(str(db), snapshot.BUCKET, "vectors.sqlite")
            c.upload_file(str(src / "weights" / "model.bin"), snapshot.BUCKET, "model.bin")
            c.put_object(Bucket=snapshot.BUCKET, Key="MANIFEST.json", Body=json.dumps(meta).encode())
    return meta
