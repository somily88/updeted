"""Small persistent safety controls shared by Spot and Futures engines."""
import datetime
import os
from database import get_db_connection


def daily_loss_status(db_path, market, equity, limit_pct):
    """Return a persistent UTC day-start equity guard; fail closed on invalid equity."""
    equity = float(equity or 0.0)
    limit_pct = float(limit_pct)
    if equity <= 0 or limit_pct <= 0:
        return {"market": market, "blocked": True, "equity": equity,
                "baseline": None, "loss_pct": None, "limit_pct": limit_pct,
                "reason": "رصيد الحساب غير متاح؛ أُوقف فتح صفقات جديدة احترازيًا"}

    day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    with get_db_connection(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS daily_equity_baselines (
            market TEXT NOT NULL, day TEXT NOT NULL, equity REAL NOT NULL,
            PRIMARY KEY (market, day))""")
        row = conn.execute(
            "SELECT equity FROM daily_equity_baselines WHERE market=? AND day=?",
            (market, day)).fetchone()
        if row is None:
            conn.execute(
                "INSERT OR IGNORE INTO daily_equity_baselines (market, day, equity) VALUES (?, ?, ?)",
                (market, day, equity))
            baseline = equity
        else:
            baseline = float(row[0])
    loss_pct = max(0.0, (baseline - equity) / baseline * 100.0)
    blocked = loss_pct >= limit_pct
    return {"market": market, "blocked": blocked, "equity": equity,
            "baseline": baseline, "loss_pct": loss_pct, "limit_pct": limit_pct,
            "day_utc": day,
            "reason": ("تم بلوغ حد الخسارة اليومية" if blocked else "ضمن حد الخسارة اليومية")}


MAX_PORTFOLIO_OPEN_RISK_PCT = float(os.getenv("MAX_PORTFOLIO_OPEN_RISK_PCT", "8.0"))
MAX_CORRELATED_CLUSTER_RISK_PCT = float(os.getenv("MAX_CORRELATED_CLUSTER_RISK_PCT", "4.0"))


def risk_cluster(symbol):
    """Conservative coarse groups for correlated open-risk limits."""
    base = str(symbol or "").split("/")[0].split(":")[0].upper()
    if base in {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "LINK",
                "AVAX", "DOT", "LTC", "BCH", "TRX", "NEAR", "SPCXB"}:
        return "crypto"
    if base in {"XAU", "PAXG", "CL", "BZ", "OIL"}:
        return "commodities"
    if base in {"AAPL", "TSLA", "SNDK", "META", "AMD", "MSFT", "MU",
                "NVDA", "BE", "AMZN", "SPCX"}:
        return "us_equities"
    return "other:" + (base or "unknown")


def portfolio_risk_status(open_risks, candidate, total_equity,
                          max_total_pct=None, max_cluster_pct=None):
    """Check proposed risk against total and correlated-cluster limits.

    Each risk item is a mapping with symbol, market, and non-negative risk_usdt.
    The caller supplies Spot and Futures positions together, so separate venue
    position limits cannot hide correlated exposure across accounts.
    """
    equity = float(total_equity or 0.0)
    total_limit = float(max_total_pct or MAX_PORTFOLIO_OPEN_RISK_PCT)
    cluster_limit = float(max_cluster_pct or MAX_CORRELATED_CLUSTER_RISK_PCT)
    candidate_risk = float((candidate or {}).get("risk_usdt") or 0.0)
    if equity <= 0 or candidate_risk <= 0 or total_limit <= 0 or cluster_limit <= 0:
        return {"allowed": False, "reason": "تعذر حساب مخاطرة المحفظة؛ فُتح حاجز الأمان",
                "equity": equity, "open_risk_usdt": None, "projected_risk_usdt": None,
                "cluster": risk_cluster((candidate or {}).get("symbol")),
                "cluster_risk_usdt": None}

    items = list(open_risks or []) + [candidate]
    total_risk = sum(max(0.0, float(item.get("risk_usdt") or 0.0)) for item in items)
    cluster = risk_cluster(candidate.get("symbol"))
    cluster_risk = sum(max(0.0, float(item.get("risk_usdt") or 0.0))
                       for item in items if risk_cluster(item.get("symbol")) == cluster)
    total_pct = total_risk / equity * 100.0
    cluster_pct = cluster_risk / equity * 100.0
    reason = "ضمن حدود المخاطرة المجمعة"
    allowed = True
    if total_pct > total_limit:
        allowed = False
        reason = "تجاوز حد المخاطرة المفتوحة للمحفظة ({:.2f}% من {:.2f}%)".format(total_pct, total_limit)
    elif cluster_pct > cluster_limit:
        allowed = False
        reason = "تجاوز حد مخاطر المجموعة المترابطة {} ({:.2f}% من {:.2f}%)".format(
            cluster, cluster_pct, cluster_limit)
    return {"allowed": allowed, "reason": reason, "equity": equity,
            "open_risk_usdt": round(total_risk - candidate_risk, 8),
            "projected_risk_usdt": round(total_risk, 8),
            "projected_risk_pct": round(total_pct, 5),
            "cluster": cluster, "cluster_risk_usdt": round(cluster_risk, 8),
            "cluster_risk_pct": round(cluster_pct, 5),
            "max_total_pct": total_limit, "max_cluster_pct": cluster_limit}
