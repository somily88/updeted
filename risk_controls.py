"""Small persistent safety controls shared by Spot and Futures engines."""
import datetime
import sqlite3


def daily_loss_status(db_path, market, equity, limit_pct):
    """Return a persistent UTC day-start equity guard; fail closed on invalid equity."""
    equity = float(equity or 0.0)
    limit_pct = float(limit_pct)
    if equity <= 0 or limit_pct <= 0:
        return {"market": market, "blocked": True, "equity": equity,
                "baseline": None, "loss_pct": None, "limit_pct": limit_pct,
                "reason": "𘀱𘀵𙀊𘀯 𘀧𙀄𘀭𘀳𘀧𘀨 𘀺𙀊𘀱 𙀅𘀪𘀧𘀭𘀛 𘀣𙀏𙀈𙀂𙀁 𙀁𘀪𘀭 𘀵𙀁𙀂𘀧𘀪 𘀬𘀯𙀊𘀯𘀩 𘀧𘀭𘀪𘀱𘀧𘀲𙀊𙀋𘀧"}

    day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        with conn:
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
                "reason": ("𘀪𙀅 𘀨𙀄𙀈𘀺 𘀭𘀯 𘀧𙀄𘀮𘀳𘀧𘀱𘀩 𘀧𙀄𙀊𙀈𙀅𙀊𘀩" if blocked else "𘀶𙀅𙀆 𘀭𘀯 𘀧𙀄𘀮𘀳𘀧𘀱𘀩 𘀧𙀄𙀊𙀈𙀅𙀊𘀩")}
    finally:
        conn.close()
