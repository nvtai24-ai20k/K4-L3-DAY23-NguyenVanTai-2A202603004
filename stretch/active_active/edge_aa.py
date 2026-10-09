"""Stretch 3 — Edge active-active: chia 50/50 cho 2 region, lỗi thì retry region còn lại.

Không sửa edge/proxy.py (active-passive). Chạy song song ở cổng khác:
    python3 -m uvicorn stretch.active_active.edge_aa:app --host 127.0.0.1 --port 8090

Khác active-passive:
  * không có file active_region / DNS cutover — cả 2 region đều nhận traffic thật
  * region lỗi (timeout, refused, 503) -> request đó retry ngay sang region kia
  * circuit breaker: TRIP_AFTER lần fail liên tiếp -> bỏ region đó COOLDOWN giây, để user
    không phải trả timeout cho mỗi request rơi vào region chết; hết cooldown thì thử lại 1 request
"""
import itertools
import os
import time

import httpx
from fastapi import FastAPI, Response

UPSTREAM = {"a": os.environ.get("REGION_A_URL", "http://127.0.0.1:8001"),
            "b": os.environ.get("REGION_B_URL", "http://127.0.0.1:8002")}
TIMEOUT = float(os.environ.get("EDGE_TIMEOUT_SECONDS", "1.0"))
TRIP_AFTER = int(os.environ.get("CB_TRIP_AFTER", "2"))
COOLDOWN = float(os.environ.get("CB_COOLDOWN_SECONDS", "10"))

app = FastAPI(title="edge-active-active")
_rr = itertools.cycle(["a", "b"])
_cb = {r: {"fails": 0, "open_until": 0.0} for r in UPSTREAM}


def order() -> list[str]:
    """Round-robin 50/50, bỏ qua region đang mở circuit (nếu cả 2 đều mở thì vẫn thử cả 2)."""
    first = next(_rr)
    both = [first, "b" if first == "a" else "a"]
    now = time.time()
    closed = [r for r in both if _cb[r]["open_until"] <= now]
    return closed or both


@app.get("/v1/infer")
def infer(q: str = "hoa don thang 7", response: Response = None):
    t0, tried, err = time.time(), [], None
    for region in order():
        tried.append(region)
        try:
            with httpx.Client(timeout=TIMEOUT) as c:
                r = c.get(f"{UPSTREAM[region]}/v1/infer", params={"q": q})
            if r.status_code == 200:
                _cb[region].update(fails=0, open_until=0.0)
                response.status_code = 200
                return {"edge_region": region, "tried": tried,
                        "edge_latency_ms": round((time.time() - t0) * 1000, 1), **r.json()}
            err = f"http_{r.status_code}"
        except Exception as e:  # refused (stop) hoặc timeout (netblock)
            err = type(e).__name__
        cb = _cb[region]
        cb["fails"] += 1
        if cb["fails"] >= TRIP_AFTER:
            cb["open_until"] = time.time() + COOLDOWN
    response.status_code = 503
    return {"edge_region": tried[-1], "tried": tried, "error": err,
            "edge_latency_ms": round((time.time() - t0) * 1000, 1)}


@app.get("/edge/state")
def edge_state():
    now = time.time()
    return {"mode": "active-active", "timeout_s": TIMEOUT, "trip_after": TRIP_AFTER,
            "cooldown_s": COOLDOWN,
            "circuit": {r: {"fails": c["fails"], "open": c["open_until"] > now} for r, c in _cb.items()}}
