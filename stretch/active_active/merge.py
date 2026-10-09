"""Stretch 3 — Anti-entropy 2 chiều giữa vector DB của 2 region, giải quyết conflict bằng LWW.

Active-active = cả 2 region cùng nhận ingest. Hai loại khác biệt cần hội tụ:
  * doc chỉ có ở 1 bên (doc_id có prefix region, vd live-a-..., live-b-...) -> copy sang bên kia.
    Đây là phần lớn traffic và KHÔNG phải conflict: append-only + id duy nhất = union là đủ.
  * cùng doc_id nhưng nội dung khác (vd ticket bị sửa ở cả 2 region trong lúc partition)
    -> CONFLICT. Luật: Last-Writer-Wins theo ingested_at; hoà thì region "a" thắng
    (tie-break cố định để 2 bên luôn chọn cùng một bản -> hội tụ).

Không merge khi 1 region không trả lời /healthz: trong outage thật bạn không đọc được đĩa của
region chết. Hai bên phân kỳ trong lúc partition và hội tụ lại sau khi region hồi phục.

    python3 stretch/active_active/merge.py --every 5 --duration 100 --out stretch/results/active-active/merge.jsonl
"""
import argparse
import json
import pathlib
import sqlite3
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
DB = {r: pathlib.Path(f"state/region-{r}/vectors.sqlite") for r in URL}


def rows(db: pathlib.Path) -> dict:
    con = sqlite3.connect(db, timeout=5)
    try:
        return {d: (body, emb, ts) for d, body, emb, ts in
                con.execute("SELECT doc_id, body, embedding, ingested_at FROM docs")}
    finally:
        con.close()


def winner(x: tuple, y: tuple) -> str:
    """LWW: ingested_at lớn hơn thắng; hoà -> 'a'."""
    return "b" if y[2] > x[2] else "a"


def merge_once(dbs: dict = DB) -> dict:
    ra, rb = rows(dbs["a"]), rows(dbs["b"])
    to = {"a": [], "b": []}
    conflicts = []
    for doc_id in ra.keys() | rb.keys():
        x, y = ra.get(doc_id), rb.get(doc_id)
        if x == y:
            continue
        if y is None:
            to["b"].append((doc_id, *x))
        elif x is None:
            to["a"].append((doc_id, *y))
        else:
            w = winner(x, y)
            win = x if w == "a" else y
            to["b" if w == "a" else "a"].append((doc_id, *win))
            conflicts.append({"doc_id": doc_id, "winner": w, "a_ts": x[2], "b_ts": y[2],
                              "a_body": x[0][:40], "b_body": y[0][:40]})
    for r, batch in to.items():
        if batch:
            con = sqlite3.connect(dbs[r], timeout=5)
            con.executemany("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)", batch)
            con.commit()
            con.close()
    return {"copied_to_a": len(to["a"]), "copied_to_b": len(to["b"]), "conflicts": conflicts,
            "count_a": len(ra) + len(to["a"]), "count_b": len(rb) + len(to["b"])}


def alive(region: str) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=1.0).status_code == 200
    except Exception:
        return False


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--every", type=float, default=5)
    p.add_argument("--duration", type=float, default=100)
    p.add_argument("--out", default="stretch/results/active-active/merge.jsonl")
    a = p.parse_args()
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + a.duration
    with out.open("a") as f:
        while time.time() < end:
            t = time.time()
            up = {r: alive(r) for r in URL}
            rec = {"ts": t, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)), "alive": up}
            if all(up.values()):
                rec.update(event="merge", **merge_once())
            else:
                rec.update(event="skip_partition",
                           reason=f"region {[r for r, ok in up.items() if not ok]} khong tra loi")
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print("MERGE", json.dumps(rec))
            time.sleep(max(0.0, a.every - (time.time() - t)))
