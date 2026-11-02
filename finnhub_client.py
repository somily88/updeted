"""Finnhub integration: extra news, economic calendar and earnings calendar.

Every public function degrades gracefully: errors are recorded in ``state`` and
never raised to the scanner. Free tier limit is 60 calls/min; we stay well
below with a token bucket (FINNHUB_MAX_CALLS_PER_MIN, default 30) and caching.
"""
import datetime
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

BASE = "https://finnhub.io/api/v1"


def _env_bool(name, default):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip().strip('"').strip("'")
FINNHUB_ENABLED = _env_bool("FINNHUB_ENABLED", True) and bool(FINNHUB_API_KEY)
FINNHUB_EVENT_PAUSE_MIN = float(os.getenv("FINNHUB_EVENT_PAUSE_MIN", "30") or 0)
FINNHUB_EARNINGS_BLOCK_H = float(os.getenv("FINNHUB_EARNINGS_BLOCK_H", "24") or 0)
FINNHUB_MAX_CALLS_PER_MIN = max(1, min(55, int(os.getenv("FINNHUB_MAX_CALLS_PER_MIN", "30") or 30)))
FINNHUB_STOCKS = [s.strip().upper() for s in os.getenv("FINNHUB_STOCKS", "META,NVDA,TSLA,AMZN,MU").split(",") if s.strip()]
FINNHUB_EVENT_KEYWORDS = [s.strip().lower() for s in os.getenv(
    "FINNHUB_EVENT_KEYWORDS",
    "cpi,fomc,fed interest rate,interest rate decision,nonfarm,non farm,non-farm,unemployment rate,ppi,gdp,pce,retail sales"
).split(",") if s.strip()]

CACHE_TTL = {"news": 15 * 60, "company": 30 * 60, "economic": 3 * 3600, "earnings": 6 * 3600}

state = {
    "enabled": FINNHUB_ENABLED, "has_key": bool(FINNHUB_API_KEY),
    "last_fetch": None, "calls": 0, "errors": {}, "endpoints": {},
    "economic_available": None, "events": [], "earnings": [], "news_count": 0,
}
_cache = {}
_lock = threading.Lock()
_calls = []
_logged_403 = set()


def _now():
    return time.time()


def _rate_ok():
    with _lock:
        t = _now()
        while _calls and t - _calls[0] > 60:
            _calls.pop(0)
        if len(_calls) >= FINNHUB_MAX_CALLS_PER_MIN:
            return False
        _calls.append(t)
        return True


def _http_get(path, params):
    """Overridable in tests."""
    url = "{}/{}?{}".format(BASE, path, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"X-Finnhub-Token": FINNHUB_API_KEY, "User-Agent": "crypto-bot"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def _get(name, path, params, ttl):
    """Cached GET. Returns parsed JSON or None. Never raises."""
    if not FINNHUB_ENABLED:
        return None
    key = (path, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and _now() - hit[0] < ttl:
        return hit[1]
    if name in _logged_403:
        return None
    if not _rate_ok():
        state["errors"][name] = "rate limit (local)"
        return hit[1] if hit else None
    try:
        data = _http_get(path, params)
        state["calls"] += 1
        state["endpoints"][name] = "ok"
        state["errors"].pop(name, None)
        _cache[key] = (_now(), data)
        state["last_fetch"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        return data
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            state["endpoints"][name] = "unavailable ({})".format(e.code)
            state["errors"][name] = "HTTP {} (غير متاح في الخطة المجانية)".format(e.code)
            if name not in _logged_403:
                _logged_403.add(name)
                print("📅 [FINNHUB] {} غير متاح (HTTP {}) - تم التعطيل لهذه الجلسة".format(name, e.code))
        else:
            state["errors"][name] = "HTTP {}".format(e.code)
        return hit[1] if hit else None
    except Exception as e:
        state["errors"][name] = type(e).__name__
        return hit[1] if hit else None


# ------------------------------------------------------------------ news
def fetch_news(window_hours=6, symbols=None):
    """Returns list of (datetime_utc, source, headline) from Finnhub."""
    if not FINNHUB_ENABLED:
        return []
    cutoff = _now() - window_hours * 3600
    out = []
    for cat in ("crypto", "general"):
        data = _get("news_" + cat, "news", {"category": cat}, CACHE_TTL["news"]) or []
        for it in data if isinstance(data, list) else []:
            if float(it.get("datetime") or 0) >= cutoff and (it.get("headline") or "").strip():
                out.append((it["datetime"], "Finnhub/" + (it.get("source") or cat), it["headline"].strip()))
    today = datetime.date.today()
    frm = (today - datetime.timedelta(days=2)).isoformat()
    for sym in (symbols or FINNHUB_STOCKS):
        data = _get("company_news", "company-news", {"symbol": sym, "from": frm, "to": today.isoformat()},
                    CACHE_TTL["company"]) or []
        n = 0
        for it in data if isinstance(data, list) else []:
            if n >= 3:
                break
            if float(it.get("datetime") or 0) >= cutoff and (it.get("headline") or "").strip():
                out.append((it["datetime"], "Finnhub/" + sym, it["headline"].strip()))
                n += 1
    out.sort(key=lambda x: x[0], reverse=True)
    state["news_count"] = len(out)
    return [(datetime.datetime.fromtimestamp(t, datetime.timezone.utc), s, h) for t, s, h in out]


def dedupe(items):
    """items: (ts, source, title). Removes near-identical titles (case/punct)."""
    seen, out = set(), []
    for ts, src, title in items:
        k = "".join(ch for ch in title.lower() if ch.isalnum())[:80]
        if k and k not in seen:
            seen.add(k)
            out.append((ts, src, title))
    return out


# ------------------------------------------------------------- calendars
def _parse_event_time(s):
    try:
        dt = datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=datetime.timezone.utc).timestamp()
    except Exception:
        return None


def refresh_calendars():
    if not FINNHUB_ENABLED:
        return
    today = datetime.date.today()
    data = _get("economic", "calendar/economic",
                {"from": (today - datetime.timedelta(days=1)).isoformat(),
                 "to": (today + datetime.timedelta(days=7)).isoformat()}, CACHE_TTL["economic"])
    if data is None and "economic" in _logged_403:
        state["economic_available"] = False
    elif isinstance(data, dict):
        state["economic_available"] = True
        events = []
        for ev in data.get("economicCalendar") or []:
            if (ev.get("country") or "").upper() != "US":
                continue
            name = (ev.get("event") or "")
            impact = str(ev.get("impact") or "").lower()
            if impact != "high" and not any(k in name.lower() for k in FINNHUB_EVENT_KEYWORDS):
                continue
            ts = _parse_event_time(ev.get("time") or "")
            if ts:
                events.append({"event": name, "ts": ts, "impact": impact or "high"})
        events.sort(key=lambda e: e["ts"])
        state["events"] = events
    earn = []
    frm, to = (today - datetime.timedelta(days=2)).isoformat(), (today + datetime.timedelta(days=21)).isoformat()
    for sym in FINNHUB_STOCKS:
        d = _get("earnings", "calendar/earnings", {"symbol": sym, "from": frm, "to": to}, CACHE_TTL["earnings"])
        for e in (d or {}).get("earningsCalendar") or [] if isinstance(d, dict) else []:
            if (e.get("symbol") or "").upper() != sym or not e.get("date"):
                continue
            hour = (e.get("hour") or "").lower()
            # bmo ~ 13:30 UTC, amc ~ 21:00 UTC, unknown -> 16:00 UTC
            hh = {"bmo": 13.5, "amc": 21.0}.get(hour, 16.0)
            try:
                d0 = datetime.datetime.strptime(e["date"], "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            earn.append({"symbol": sym, "date": e["date"], "hour": hour,
                         "ts": d0.timestamp() + hh * 3600})
    earn.sort(key=lambda e: e["ts"])
    state["earnings"] = earn


def stock_from_symbol(symbol):
    base = (symbol or "").split("/")[0].upper()
    for suf in ("USDT", "USDC"):
        if base.endswith(suf) and len(base) > len(suf):
            base = base[: -len(suf)]
    return base if base in FINNHUB_STOCKS else None


def entry_block_reason(symbol, now=None):
    """Arabic reason string if new entries on symbol must be blocked, else None."""
    if not FINNHUB_ENABLED:
        return None
    try:
        now = now or _now()
        if FINNHUB_EVENT_PAUSE_MIN > 0:
            w = FINNHUB_EVENT_PAUSE_MIN * 60
            for ev in state.get("events") or []:
                if abs(ev["ts"] - now) <= w:
                    return "إيقاف مؤقت حول حدث اقتصادي أمريكي: {} ({} دقيقة قبل/بعد)".format(
                        ev["event"], int(FINNHUB_EVENT_PAUSE_MIN))
        stock = stock_from_symbol(symbol)
        if stock and FINNHUB_EARNINGS_BLOCK_H > 0:
            w = FINNHUB_EARNINGS_BLOCK_H * 3600
            for e in state.get("earnings") or []:
                if e["symbol"] == stock and abs(e["ts"] - now) <= w:
                    return "حظر الدخول قرب إعلان أرباح {} بتاريخ {} (±{} ساعة)".format(
                        stock, e["date"], int(FINNHUB_EARNINGS_BLOCK_H))
    except Exception:
        logger.debug("finnhub gate error", exc_info=True)
    return None


def snapshot():
    now = _now()
    fmt = lambda ts: datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    return {
        "enabled": FINNHUB_ENABLED, "has_key": bool(FINNHUB_API_KEY),
        "last_fetch": state["last_fetch"], "calls": state["calls"],
        "economic_available": state["economic_available"],
        "endpoints": dict(state["endpoints"]), "errors": dict(state["errors"]),
        "news_count": state["news_count"],
        "settings": {"event_pause_min": FINNHUB_EVENT_PAUSE_MIN, "earnings_block_h": FINNHUB_EARNINGS_BLOCK_H,
                     "stocks": FINNHUB_STOCKS, "max_calls_per_min": FINNHUB_MAX_CALLS_PER_MIN},
        "upcoming_events": [{"event": e["event"], "time": fmt(e["ts"]), "impact": e["impact"]}
                            for e in state["events"] if e["ts"] >= now - 3600][:15],
        "upcoming_earnings": [{"symbol": e["symbol"], "date": e["date"], "hour": e["hour"], "time": fmt(e["ts"])}
                              for e in state["earnings"] if e["ts"] >= now - 86400][:15],
    }
