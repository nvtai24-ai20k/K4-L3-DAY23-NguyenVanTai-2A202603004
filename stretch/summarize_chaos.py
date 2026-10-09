"""Tổng hợp kết quả stretch/chaos_random.sh: bảng từng run + mean/stdev RTO, theo mode.

    python3 stretch/summarize_chaos.py stretch/results/chaos-random
"""
import json
import pathlib
import statistics
import sys


def stats(xs: list[float]) -> dict:
    return {"n": len(xs), "mean_s": round(statistics.mean(xs), 2) if xs else None,
            "stdev_s": round(statistics.stdev(xs), 2) if len(xs) > 1 else None,
            "min_s": min(xs, default=None), "max_s": max(xs, default=None)}


def main(out: pathlib.Path):
    rows = []
    for d in sorted(out.glob("run-*"), key=lambda p: int(p.name.split("-")[1])):
        p = json.loads((d / "params.json").read_text())
        m = json.loads((d / "measure.json").read_text())
        b = m["breakdown_seconds_from_t0"]
        rows.append({**p, "valid": m["valid"], "warnings": len(m["warnings"]),
                     "first_fail_s": b["user_thay_loi_dau_tien"], "detect_s": b["health_check_phat_hien"],
                     "cutover_s": b["dns_cutover"], "rto_s": m["rto_measured_s"],
                     "verdict": m["rto_verdict"], "rpo_s": m["rpo_at_restore_s"],
                     "docs_lost": m["docs_lost"], "requests_failed": m["requests_failed"]})
    ok = [r for r in rows if r["valid"] and r["rto_s"] is not None]
    summary = {"all": stats([r["rto_s"] for r in ok]),
               "by_mode": {mode: stats([r["rto_s"] for r in ok if r["mode"] == mode])
                           for mode in ("stop", "netblock")},
               "detect": stats([r["detect_s"] for r in ok if r["detect_s"] is not None]),
               "rpo": stats([r["rpo_s"] for r in ok if r["rpo_s"] is not None]),
               "runs": rows}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))

    cols = ["run", "mode", "kill_after_s", "first_fail_s", "detect_s", "cutover_s", "rto_s",
            "verdict", "rpo_s", "docs_lost", "requests_failed", "valid", "warnings"]
    md = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    md += ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows]
    md += ["", "| Nhóm | n | mean RTO | stdev | min | max |", "|---|---|---|---|---|---|"]
    for name, s in [("tất cả", summary["all"]), ("stop", summary["by_mode"]["stop"]),
                    ("netblock", summary["by_mode"]["netblock"])]:
        md.append(f"| {name} | {s['n']} | {s['mean_s']}s | {s['stdev_s']}s | {s['min_s']}s | {s['max_s']}s |")
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "stretch/results/chaos-random"))
