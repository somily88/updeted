"""Massive (formerly Polygon.io) integration for TradFi perps underlyings.

Used endpoints (all available on the free/basic plan, verified):
  * /v1/marketstatus/now         US market status (open / extended-hours / closed)
  * /v1/marketstatus/upcoming    upcoming holidays / early closes
  * /v2/reference/news?ticker=   per-ticker news with sentiment insights
  * /v2/aggs/ticker/{T}/range/1/day/...  daily bars -> SMA trend context
Not used: /v2/snapshot (HTTP 403 on free plan).

Design: a background job (``refresh``) does the network calls within a strict
token bucket (MASSIVE_MAX_CALLS_PER_MIN, default 4 since free tier = 5/min).
Scan-time gates only read cached ``state`` so scans are never slowed or broken.
Every public function swallows errors.
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

BASE = os.getenv("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/")


def _env_bool(name, default):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return float(default)


MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "").strip().strip('"').strip("'")
MASSIVE_ENABLED = _env_bool("MASSIVE_ENABLED", True) and bool(MASSIVE_API_KEY)
MASSIVE_MAX_CALLS_PER_MIN = max(1, min(100, int(_env_float("MASSIVE_MAX_CALLS_PER_MIN", 4))))
MASSIVE_STOCKS = [s.strip().upper() for s in os.getenv("MASSIVE_STOCKS", "META,NVDA,TSLA,AMZN,MU").split(",") if s.strip()]
MASSIVE_MARKET_HOURS_GUARD = _env_bool("MASSIVE_MARKET_HOURS_GUARD", False)
MASSIVE_OPEN_BUFFER_MIN = _env_float("MASSIVE_OPEN_BUFFER_MIN", 15)
MASSIVE_UNDERLYING_CONFIRM = _env_bool("MASSIVE_UNDERLYING_CONFIRM", False)
MASSIVE_SMA_DAYS = max(5, int(_env_float("MASSIVE_SMA_DAYS", 50)))
MASSIVE_NEWS_ENABLED = _env_bool("MASSIVE_NEWS_ENABLED", True)

TTL = {"status": 5 * 60, "upcoming": 12 * 3600, "news": 30 * 60, "aggs": 6 * 3600}

state = {
    "last_fetch": None, "calls": 0, "errors": {}, "endpoints": {},
    "market": None, "market_fetched": None, "upcoming": [],
    "news": {}, "trend": {}, "news_count": 0, "open_since": None,
}
_cache = {}
_lock = threading.Lock()
_calls = []
_unavailable = set()


def _now():
    return time.time()


def _rate_ok():
    with _lock:
        t = _now()
        while _calls and t - _calls[0] > 60:
            _calls.pop(0)
        if len(_calls) >= MASSIVE_MAX_CALLS_PER_MIN:
            return False
        _calls.append(t)
        return True


def _http_get(path, params):
    """Overridable in tests. API key is sent as a header, never logged."""
    url = "{}/{}".format(BASE, path.lstrip("/"))
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + MASSIVE_API_KEY,
                                               "User-Agent": "crypto-bot"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def _fresh(key, ttl):
    hit = _cache.get(key)
    return hit is not None and _now() - hit[0] < ttl


def _get(name, path, params, ttl):
    """Cached GET. Returns parsed JSON or None. Never raises."""
    if not MASSIVE_ENABLED:
        return None
    key = (path, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and _now() - hit[0] < ttl:
        return hit[1]
    if name in _unavailable:
        return hit[1] if hit else None
    if not _rate_ok():
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
            _unavailable.add(name)
            state["endpoints"][name] = "unavailable ({})".format(e.code)
            state["errors"][name] = "HTTP {} (غير متاح في خطتك)".format(e.code)
            print("📈 [MASSIVE] {} غير متاح (HTTP {}) - تم التعطيل لهذه الجلسة".format(name, e.code))
        elif e.code == 429:
            state["errors"][name] = "HTTP 429 (تجاوز حد الطلبات)"
            with _lock:  # back off: fill the bucket for this minute
                _calls.extend([_now()] * MASSIVE_MAX_CALLS_PER_MIN)
        else:
            state["errors"][name] = "HTTP {}".format(e.code)
        return hit[1] if hit else None
    except Exception as e:
        state["errors"][name] = type(e).__name__
        return hit[1] if hit else None


# ------------------------------------------------------------- refreshers
def _refresh_status():
    d = _get("market_status", "v1/marketstatus/now", {}, TTL["status"])
    if isinstance(d, dict):
        prev = (state.get("market") or {}).get("market")
        state["market"] = {
            "market": d.get("market"), "early_hours": bool(d.get("earlyHours")),
            "after_hours": bool(d.get("afterHours")),
            "nasdaq": (d.get("exchanges") or {}).get("nasdaq"),
            "nyse": (d.get("exchanges") or {}).get("nyse"),
            "server_time": d.get("serverTime"),
        }
        state["market_fetched"] = _now()
        if d.get("market") == "open" and prev != "open":
            state["open_since"] = _now()
        elif d.get("market") != "open":
            state["open_since"] = None


def _refresh_upcoming():
    d = _get("market_upcoming", "v1/marketstatus/upcoming", {}, TTL["upcoming"])
    if isinstance(d, list):
        seen, out = set(), []
        for it in d:
            k = (it.get("date"), it.get("name"), it.get("status"))
            if k in seen:
                continue
            seen.add(k)
            out.append({"date": it.get("date"), "name": it.get("name"), "status": it.get("status"),
                        "open": it.get("open"), "close": it.get("close")})
        state["upcoming"] = out[:8]


def _refresh_news(sym):
    d = _get("news", "v2/reference/news", {"ticker": sym, "limit": 10, "order": "desc", "sort": "published_utc"},
             TTL["news"])
    if not isinstance(d, dict):
        return
    items = []
    for it in d.get("results") or []:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        sent = None
        for ins in it.get("insights") or []:
            if (ins.get("ticker") or "").upper() == sym:
                sent = ins.get("sentiment")
                break
        items.append({"ts": it.get("published_utc"), "title": title,
                      "publisher": (it.get("publisher") or {}).get("name") or "Massive",
                      "sentiment": sent, "url": it.get("article_url")})
    state["news"][sym] = items


def _sma_trend(closes, n):
    if len(closes) < n:
        return None
    sma = sum(closes[-n:]) / n
    last = closes[-1]
    return {"close": round(last, 4), "sma": round(sma, 4), "above": last > sma,
            "dist_pct": round((last / sma - 1) * 100, 2) if sma else None}


def _refresh_trend(sym):
    to = datetime.date.today()
    frm = to - datetime.timedelta(days=int(MASSIVE_SMA_DAYS * 1.6) + 10)
    d = _get("aggs", "v2/aggs/ticker/{}/range/1/day/{}/{}".format(sym, frm.isoformat(), to.isoformat()),
             {"adjusted": "true", "sort": "asc", "limit": 500}, TTL["aggs"])
    if isinstance(d, dict):
        closes = [float(r["c"]) for r in d.get("results") or [] if r.get("c") is not None]
        t = _sma_trend(closes, MASSIVE_SMA_DAYS)
        if t:
            t["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            state["trend"][sym] = t


def refresh():
    """Called by scheduler. Fetches whatever is stale, within the rate budget."""
    if not MASSIVE_ENABLED:
        return
    try:
        _refresh_status()
        _refresh_upcoming()
        for sym in MASSIVE_STOCKS:
            _refresh_trend(sym)
        if MASSIVE_NEWS_ENABLED:
            for sym in MASSIVE_STOCKS:
                _refresh_news(sym)
        state["news_count"] = sum(len(v) for v in state["news"].values())
    except Exception:
        logger.debug("massive refresh error", exc_info=True)


# ------------------------------------------------------------------ news
def _parse_ts(s):
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def fetch_news(window_hours=6, per_ticker=3):
    """Cached news as list of (datetime_utc, source, headline). No network here."""
    if not (MASSIVE_ENABLED and MASSIVE_NEWS_ENABLED):
        return []
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=window_hours)
    out = []
    for sym, items in list(state["news"].items()):
        n = 0
        for it in items:
            ts = _parse_ts(it.get("ts"))
            if ts is None or ts < cutoff:
                continue
            out.append((ts, "Massive/" + sym, it["title"]))
            n += 1
            if n >= per_ticker:
                break
    out.sort(key=lambda x: x[0], reverse=True)
    return out


# ------------------------------------------------------------------ gates
def stock_from_symbol(symbol):
    base = (symbol or "").split("/")[0].upper()
    for suf in ("USDT", "USDC"):
        if base.endswith(suf) and len(base) > len(suf):
            base = base[: -len(suf)]
    return base if base in MASSIVE_STOCKS else None


def entry_block_reason(symbol, side=None, now=None):
    """Arabic reason if a new entry must be blocked, else None. Reads cache only."""
    if not MASSIVE_ENABLED:
        return None
    try:
        stock = stock_from_symbol(symbol)
        if not stock:
            return None
        now = now or _now()
        m = state.get("market")
        fresh = m and state.get("market_fetched") and now - state["market_fetched"] < 30 * 60
        if MASSIVE_MARKET_HOURS_GUARD and fresh:
            if m.get("market") != "open":
                return "السوق الأمريكي مغلق ({}) - حظر الدخول على {} (Massive)".format(
                    _status_ar(m), stock)
            since = state.get("open_since")
            if MASSIVE_OPEN_BUFFER_MIN > 0 and since and now - since < MASSIVE_OPEN_BUFFER_MIN * 60:
                return "أول {} دقيقة بعد افتتاح السوق الأمريكي - حظر الدخول على {} (Massive)".format(
                    int(MASSIVE_OPEN_BUFFER_MIN), stock)
        if MASSIVE_UNDERLYING_CONFIRM and side in ("long", "short", "buy"):
            t = state["trend"].get(stock)
            if t:
                long_side = side in ("long", "buy")
                if long_side and not t["above"]:
                    return "سهم {} تحت متوسط {} يوم - لا تأكيد للشراء (Massive)".format(stock, MASSIVE_SMA_DAYS)
                if not long_side and t["above"]:
                    return "سهم {} فوق متوسط {} يوم - لا تأكيد للبيع (Massive)".format(stock, MASSIVE_SMA_DAYS)
    except Exception:
        logger.debug("massive gate error", exc_info=True)
    return None


def _status_ar(m):
    if not m:
        return "غير معروف"
    if m.get("market") == "open":
        return "مفتوح"
    if m.get("early_hours"):
        return "ما قبل الافتتاح"
    if m.get("after_hours"):
        return "ما بعد الإغلاق"
    if m.get("market") == "extended-hours":
        return "تداول ممتد"
    return "مغلق"


def snapshot():
    m = state.get("market")
    return {
        "enabled": MASSIVE_ENABLED, "has_key": bool(MASSIVE_API_KEY),
        "last_fetch": state["last_fetch"], "calls": state["calls"],
        "endpoints": dict(state["endpoints"]), "errors": dict(state["errors"]),
        "market": dict(m) if m else None, "market_ar": _status_ar(m) if m else None,
        "upcoming": list(state["upcoming"]),
        "trend": {k: dict(v) for k, v in state["trend"].items()},
        "news": {k: v[:3] for k, v in state["news"].items()},
        "news_count": state["news_count"],
        "settings": {"stocks": MASSIVE_STOCKS, "max_calls_per_min": MASSIVE_MAX_CALLS_PER_MIN,
                     "market_hours_guard": MASSIVE_MARKET_HOURS_GUARD, "open_buffer_min": MASSIVE_OPEN_BUFFER_MIN,
                     "underlying_confirm": MASSIVE_UNDERLYING_CONFIRM, "sma_days": MASSIVE_SMA_DAYS,
                     "news_enabled": MASSIVE_NEWS_ENABLED},
    }
