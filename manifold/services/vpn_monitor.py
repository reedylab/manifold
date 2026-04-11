"""VPN latency sampling and gluetun control.

Manifold runs inside its own gluetun network namespace, so a ping from this
container goes through the WireGuard tunnel and gives us the true round-trip
time of the VPN. Sampled every 60 seconds and surfaced in the System Stats
tab as a 4th chart alongside CPU/RAM/Disk.

Auto-rotate cycles the WireGuard tunnel via gluetun's control API
(`PUT /v1/vpn/status`) WITHOUT recreating the gluetun container — so attached
services (manifold, downstream containers) keep their network namespace and
don't experience the zombie-attached-services problem.
"""

import logging
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import requests as http_requests

logger = logging.getLogger(__name__)

# 1440 samples = 24h of history at one sample per minute
_samples = deque(maxlen=1440)
_lock = threading.Lock()
_last_rotate_at = None


def _get_auth_and_url():
    """Resolve gluetun control config from manifold's Config singleton."""
    from manifold.config import Config
    cfg = Config()
    if not cfg.GLUETUN_CONTROL_URL:
        return None, None
    auth = (cfg.GLUETUN_CONTROL_USER, cfg.GLUETUN_CONTROL_PASS) if cfg.GLUETUN_CONTROL_USER else None
    return cfg.GLUETUN_CONTROL_URL, auth


def _ping_rtt(target: str = "1.1.1.1", timeout: int = 2) -> float | None:
    """Ping target once. Return RTT in milliseconds or None on failure."""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), target],
            capture_output=True, text=True, timeout=timeout + 2,
        )
        if r.returncode != 0:
            return None
        m = re.search(r"time=([\d.]+)", r.stdout)
        return float(m.group(1)) if m else None
    except Exception as e:
        logger.debug("[VPN-MONITOR] ping failed: %s", e)
        return None


def _fetch_exit_info() -> tuple[str, str]:
    """Return (public_ip, city) from gluetun's /v1/publicip/ip endpoint."""
    base, auth = _get_auth_and_url()
    if not base:
        return ("", "")
    try:
        r = http_requests.get(f"{base}/v1/publicip/ip", auth=auth, timeout=3)
        d = r.json()
        return (d.get("public_ip", ""), d.get("city") or d.get("region", ""))
    except Exception:
        return ("", "")


def sample_latency():
    """Take one latency sample and append to the rolling history."""
    rtt = _ping_rtt()
    ip, city = _fetch_exit_info()
    sample = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "rtt_ms": rtt,
        "ip": ip,
        "city": city,
    }
    with _lock:
        _samples.append(sample)


def get_history(minutes: int = 60) -> list[dict]:
    """Return all samples within the last N minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    with _lock:
        out = []
        for s in _samples:
            try:
                if datetime.fromisoformat(s["ts"]) >= cutoff:
                    out.append(s)
            except Exception:
                continue
        return out


def get_summary() -> dict:
    """Compute current/min/avg/max RTT plus current exit info."""
    with _lock:
        recent = list(_samples)[-60:]  # last hour at 1/min
    if not recent:
        return {
            "current_rtt_ms": None,
            "min_rtt_ms": None,
            "avg_rtt_ms": None,
            "max_rtt_ms": None,
            "current_ip": "",
            "current_city": "",
            "sample_count": 0,
            "last_rotate_at": _last_rotate_at.isoformat() if _last_rotate_at else None,
        }
    rtts = [s["rtt_ms"] for s in recent if s["rtt_ms"] is not None]
    latest = recent[-1]
    summary = {
        "current_rtt_ms": latest["rtt_ms"],
        "current_ip": latest["ip"],
        "current_city": latest["city"],
        "sample_count": len(recent),
        "last_rotate_at": _last_rotate_at.isoformat() if _last_rotate_at else None,
    }
    if rtts:
        summary["min_rtt_ms"] = round(min(rtts), 1)
        summary["max_rtt_ms"] = round(max(rtts), 1)
        summary["avg_rtt_ms"] = round(sum(rtts) / len(rtts), 1)
    else:
        summary["min_rtt_ms"] = None
        summary["max_rtt_ms"] = None
        summary["avg_rtt_ms"] = None
    return summary


def rotate_vpn(reason: str = "manual") -> dict:
    """Cycle the WireGuard tunnel via gluetun control API.

    Returns {"ok": bool, "from": {ip, city}, "to": {ip, city}, "error"?: str}.
    """
    global _last_rotate_at
    base, auth = _get_auth_and_url()
    if not base:
        return {"ok": False, "error": "GLUETUN_CONTROL_URL not configured"}

    old_ip, old_city = _fetch_exit_info()
    logger.info("[VPN-MONITOR] Rotating VPN (%s) — current: %s in %s", reason, old_ip, old_city)

    try:
        http_requests.put(
            f"{base}/v1/vpn/status",
            json={"status": "stopped"},
            auth=auth,
            timeout=5,
        ).raise_for_status()
        time.sleep(2)
        http_requests.put(
            f"{base}/v1/vpn/status",
            json={"status": "running"},
            auth=auth,
            timeout=5,
        ).raise_for_status()

        # Poll for new IP, up to 30s
        new_ip, new_city = "", ""
        for _ in range(30):
            time.sleep(1)
            new_ip, new_city = _fetch_exit_info()
            if new_ip and new_ip != old_ip:
                break

        _last_rotate_at = datetime.now(timezone.utc)
        logger.info(
            "[VPN-MONITOR] Rotated: %s (%s) → %s (%s)",
            old_ip, old_city, new_ip, new_city,
        )

        # Take an immediate sample so the chart updates right away
        try:
            sample_latency()
        except Exception:
            pass

        return {
            "ok": True,
            "from": {"ip": old_ip, "city": old_city},
            "to": {"ip": new_ip, "city": new_city},
        }
    except Exception as e:
        logger.exception("[VPN-MONITOR] Rotate failed")
        return {"ok": False, "error": str(e)}


def maybe_auto_rotate():
    """Called by scheduler every 60s. Rotates if interval > 0 AND enough time has passed."""
    from manifold.config import get_setting
    try:
        minutes = int(get_setting("vpn_auto_rotate_minutes", "0") or "0")
    except (ValueError, TypeError):
        minutes = 0
    if minutes <= 0:
        return
    global _last_rotate_at
    if _last_rotate_at and (datetime.now(timezone.utc) - _last_rotate_at).total_seconds() < minutes * 60:
        return
    rotate_vpn(reason="auto")
