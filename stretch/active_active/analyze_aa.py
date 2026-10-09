"""Stretch 3 — đọc loadgen của drill active-active, so sánh trước/sau lúc kill region a.

    python3 stretch/active_active/analyze_aa.py stretch/results/active-active
"""
import collections
import json
import pathlib
import sys


def jsonl(p: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def pct(xs: list[float], q: int):
    s = sorted(xs)
    return round(s[max(0, -(-len(s) * q // 100) - 1)], 1) if s else None


def window(reqs: list[dict]) -> dict:
    lat = [r["latency_ms"] for r in reqs]
    longest, cur = 0.0, None  # chuỗi fail liên tiếp dài nhất (giây) = "RTO" user thấy
    for r in reqs:
        if not r["ok"]:
            cur = cur if cur is not None else r["ts"]
            longest = max(longest, r["ts"] + r["latency_ms"] / 1000 - cur)
        else:
            cur = None
    return {"requests": len(reqs), "failed": sum(not r["ok"] for r in reqs),
            "served_by": dict(collections.Counter(r.get("served_by") for r in reqs if r["ok"])),
            "p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95), "max_ms": max(lat, default=None),
            "longest_failure_streak_s": round(longest, 2)}


def main(out: pathlib.Path):
    reqs = jsonl(out / "loadgen-aa.jsonl")
    t0 = next(e["ts"] for e in jsonl(out / "chaos-events.jsonl") if e.get("action") == "kill")
    after = [r for r in reqs if r["ts"] >= t0]
    slow = [{"line": i, "t_from_kill_s": round(r["ts"] - t0, 1), "latency_ms": r["latency_ms"],
             "ok": r["ok"], "served_by": r.get("served_by")}
            for i, r in enumerate(reqs, 1) if r["ts"] >= t0 and r["latency_ms"] > 500]
    res = {"t_kill": t0, "before_kill": window([r for r in reqs if r["ts"] < t0]),
           "after_kill": window(after), "slow_requests_after_kill": slow}
    (out / "summary.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "stretch/results/active-active"))
