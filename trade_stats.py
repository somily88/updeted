"""R-multiple helpers and per-strategy performance stats (read-only)."""
import sqlite3
from collections import defaultdict


def initial_risk_usdt(entry_price, risk_distance, initial_cost):
    """USDT lost if the initial stop is hit: quantity x stop distance."""
    try:
        entry, dist, cost = float(entry_price or 0), float(risk_distance or 0), float(initial_cost or 0)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or dist <= 0 or cost <= 0:
        return None
    return cost / entry * dist


def r_multiple(net_pnl_usdt, risk_usdt):
    if not risk_usdt or float(risk_usdt) <= 0 or net_pnl_usdt is None:
        return None
    return float(net_pnl_usdt) / float(risk_usdt)


# Spot realized PnL counts only these cost qualities. /api/state and strategy
# stats both use this set so an unverified row cannot move one total and not the other.
VERIFIED_SPOT_QUALITIES = ("actual", "estimated")


def spot_verified_sql(pnl_col="pnl_usdt", quality_col="cost_quality"):
    quoted = ", ".join("'{}'".format(name) for name in VERIFIED_SPOT_QUALITIES)
    return "{} IS NOT NULL AND {} IN ({})".format(pnl_col, quality_col, quoted)


def spot_unverified_sql(pnl_col="pnl_usdt", quality_col="cost_quality"):
    quoted = ", ".join("'{}'".format(name) for name in VERIFIED_SPOT_QUALITIES + ("simulated",))
    return "{} IS NULL OR {} IS NULL OR {} NOT IN ({})".format(
        pnl_col, quality_col, quality_col, quoted)


def summarize(rows):
    """rows: iterables of dicts with market, strategy, pnl_usdt, fees_usdt, r_multiple."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row.get("market"), row.get("strategy") or "unknown")].append(row)
    out = []
    for (market, strategy), items in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        pnls = [float(i.get("pnl_usdt") or 0.0) for i in items]
        wins = [p for p in pnls if p > 0]
        losses = [-p for p in pnls if p <= 0]
        rs = [float(i["r_multiple"]) for i in items if i.get("r_multiple") is not None]
        gross_loss = sum(losses)
        out.append({
            "market": market, "strategy": strategy, "trades": len(items),
            "wins": len(wins), "win_rate_pct": round(len(wins) / len(items) * 100.0, 2),
            "net_pnl_usdt": round(sum(pnls), 4), "avg_net_usdt": round(sum(pnls) / len(items), 4),
            "avg_win_usdt": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "avg_loss_usdt": round(gross_loss / len(losses), 4) if losses else 0.0,
            "profit_factor": round(sum(wins) / gross_loss, 3) if gross_loss > 0 else None,
            "fees_usdt": round(sum(float(i.get("fees_usdt") or 0.0) for i in items), 4),
            "trades_with_r": len(rs),
            "expectancy_r": round(sum(rs) / len(rs), 4) if rs else None,
            "sample_warning": len(items) < 30,
        })
    return out


def load_strategy_stats(db_path):
    rows = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, market in (("trades", "spot"), ("futures_trades", "futures")):
            if table not in tables:
                continue
            cols = {r[1] for r in conn.execute("PRAGMA table_info({})".format(table))}
            if "pnl_usdt" not in cols:
                continue
            select = ["strategy" if "strategy" in cols else "NULL AS strategy", "pnl_usdt",
                      "fees_usdt" if "fees_usdt" in cols else "0 AS fees_usdt",
                      "r_multiple" if "r_multiple" in cols else "NULL AS r_multiple"]
            where = " WHERE pnl_usdt IS NOT NULL"
            if "excluded_from_stats" in cols:
                where += " AND COALESCE(excluded_from_stats, 0) = 0"
            if table == "trades" and "cost_quality" in cols:
                # Same verified set as get_realized_stats. Legacy/unverified rows stay out
                # until a fill reconciliation marks them actual.
                where += " AND cost_quality IN ({})".format(
                    ", ".join("'{}'".format(name) for name in VERIFIED_SPOT_QUALITIES))
            elif "cost_quality" in cols:
                where += " AND COALESCE(cost_quality, '') != 'simulated'"
            for r in conn.execute("SELECT {} FROM {}{}".format(", ".join(select), table, where)):
                rows.append(dict(r, market=market))
    return summarize(rows)
