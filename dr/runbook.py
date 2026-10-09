"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


from dr.health_checker import probe  # noqa: E402

CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH = pathlib.Path("reports/health-events.jsonl")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # không có TTY -> coi như KHÔNG đồng ý
        return False


def _jsonl(p: pathlib.Path) -> list[tuple[int, dict]]:
    if not p.exists():
        return []
    return [(i, json.loads(l)) for i, l in enumerate(p.read_text().splitlines(), 1) if l.strip()]


def _t_outage(primary: str):
    """Mốc outage thật từ chaos log (để postmortem tính độ trễ phát hiện/thông báo)."""
    kills = [e for _, e in _jsonl(CHAOS) if e.get("action") == "kill" and e.get("region") == primary]
    return kills[-1] if kills else None


def _health_alert(primary: str, since: float):
    """Alert UNHEALTHY đầu tiên của health checker cho region chính, sau mốc `since`."""
    for line, e in _jsonl(HEALTH):
        if (e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                and e.get("region") == primary and e["ts"] >= since):
            return line, e
    return None


def _alive(region: str) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=2.0).status_code == 200
    except Exception:
        return False


def _p95(xs: list[float]) -> float:
    s = sorted(xs)
    return s[max(0, -(-len(s) * 95 // 100) - 1)]


def run(primary: str, target: str, backend: str, auto: bool, wait: float = 60,
        threshold: int = 3, alert_timeout: float = 90) -> dict:
    """7 bước của runbook §4 "Region Chính Down"."""
    t_start = time.time()
    kill = _t_outage(primary)
    since = kill["ts"] if kill else t_start - 120

    # 1 — xác nhận outage: probe region chính tới khi fail `threshold` lần LIÊN TIẾP,
    #     và chờ alert của health checker (nguồn alert chính thức). Region phụ phải sống.
    fails, reason, alert = 0, None, None
    deadline = t_start + alert_timeout
    while True:
        ok, reason = probe(primary, 2.0)
        fails = 0 if ok else fails + 1
        alert = alert or _health_alert(primary, since)
        if fails >= threshold and (alert or time.time() >= deadline):
            break
        if time.time() >= deadline:
            step(1, "xac_nhan_outage", confirmed=False, primary=primary,
                 consecutive_fails=fails, last_reason=reason, alert_seen=alert is not None)
            return {"ok": False, "aborted_at": "xac_nhan_outage",
                    "reason": f"region-{primary} khong fail {threshold} lan lien tiep -> khong phai outage"}
        time.sleep(2.0)
    target_alive = _alive(target)
    step(1, "xac_nhan_outage", confirmed=True, primary=primary, consecutive_fails=fails,
         last_reason=reason, target=target, target_alive=target_alive,
         alert_seen=alert is not None, t_alert=alert[1]["ts"] if alert else None,
         health_alert_evidence=f"{HEALTH}:{alert[0]}" if alert else None)
    if not target_alive:
        return {"ok": False, "aborted_at": "xac_nhan_outage",
                "reason": f"region-{target} cung khong song -> double outage, khong failover"}

    # 2 — mở incident, bấm đồng hồ. ts dòng này luôn SAU t_outage.
    t_alert, now = (alert[1]["ts"] if alert else None), time.time()
    inc = step(2, "thong_bao_incident", severity="SEV1",
               summary=f"region-{primary} down, failover sang region-{target}",
               t_outage=kill["ts"] if kill else None, t_outage_iso=kill["iso"] if kill else None,
               t_alert=t_alert,
               notify_delay_s=None if not kill else round(now - kill["ts"], 2),
               detect_delay_s=None if not (kill and t_alert) else round(t_alert - kill["ts"], 2))

    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        step(3, "scale_gpu_pool", skipped=True, reason="operator_khong_confirm")
        return {"ok": False, "aborted_at": "confirm", "reason": "operator khong confirm"}

    # 3 — failover: gọi MỘT lần, nó tự làm 5 bước con và log vào failover-events.jsonl
    res = fo.failover(target, backend, wait)
    step(3, "scale_gpu_pool", ok=res["ok"], failover_total_s=res.get("total_s"),
         aborted_at=res.get("aborted_at"), reason=res.get("reason"))
    if not res["ok"]:
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - inc["ts"], 2),
             reason=res.get("reason"))
        return {"ok": False, "aborted_at": "scale_gpu_pool", "failover": res}

    # 4, 5 — chỉ ĐỌC kết quả của bước 3, không gọi lại failover
    restore, ready = res["steps"]["2_restore_snapshot"], res["steps"]["4_wait_ready"]
    step(4, "verify_state_replica", target=target, vector_count=ready["vector_count"],
         weights=ready["weights"], embed_model_version=restore["embed_model_version"],
         rpo_seconds=restore["rpo_seconds"], docs_lost=restore["docs_lost"])
    cut = res["steps"]["5_dns_cutover"]
    step(5, "dns_cutover", ok=True, active_from=cut["from"], active_to=cut["to"])

    # 6 — golden signals: 10 request thật vào region phụ
    lat, errors = [], 0
    for i in range(10):
        t = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", params={"q": f"golden {i}"}, timeout=3.0)
            errors += r.status_code != 200 or "error" in r.json()
        except Exception:
            errors += 1
        lat.append((time.time() - t) * 1000)
    golden = {"requests": len(lat), "p95_ms": round(_p95(lat), 1),
              "error_rate": errors / len(lat), "slo_p95_ms": 500, "slo_error_rate": 0.0}
    golden["ok"] = golden["p95_ms"] < golden["slo_p95_ms"] and golden["error_rate"] <= 0.0
    step(6, "verify_golden_signals", target=target, **golden)

    # 7 — tổng kết + lệnh đo RTO từ log (RTO thật lấy từ loadgen, không lấy từ đây)
    out = {"ok": golden["ok"], "primary": primary, "target": target,
           "elapsed_s": round(time.time() - inc["ts"], 2),
           "elapsed_since_outage_s": None if not kill else round(time.time() - kill["ts"], 2),
           "rpo_seconds": restore["rpo_seconds"], "docs_lost": restore["docs_lost"],
           "golden": golden,
           "measure_cmd": "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                          "--target-rto 300"}
    step(7, "post_incident", **out)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    p.add_argument("--wait", type=float, default=60, help="timeout cho 4_wait_ready")
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--alert-timeout", type=float, default=90)
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto, a.wait, a.threshold,
                         a.alert_timeout), indent=2))
