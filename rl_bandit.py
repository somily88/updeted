"""Contextual-bandit reinforcement learning layer (Thompson sampling).

The agent learns, per trading context, the expected realized R multiple of a
signal (fees included) and chooses one of three actions:

    skip  -> 0.0x size
    half  -> 0.5x size
    full  -> 1.0x size

Safety guarantees:
  * RL_MODE=off (default) does nothing; shadow only logs; live may skip/shrink.
  * The applied multiplier is always within [0, 1]: RL never increases size
    above what the existing risk rules already allow.
  * Live mode acts only once a context has RL_MIN_SAMPLES closed trades.
  * Every failure falls back to the unchanged 1.0x behaviour.
"""
import datetime
import math
import os
import random
import sqlite3
import threading

ACTIONS = {"skip": 0.0, "half": 0.5, "full": 1.0}
ACTION_LABELS_AR = {"skip": "تخطي", "half": "نصف الحجم", "full": "الحجم الكامل"}
MODE_LABELS_AR = {"off": "متوقف", "shadow": "وضع الظل", "live": "مفعّل"}


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


RL_MODE = os.getenv("RL_MODE", "off").strip().lower()
if RL_MODE not in ("off", "shadow", "live"):
    RL_MODE = "off"
RL_MIN_SAMPLES = max(1, int(_env_float("RL_MIN_SAMPLES", 30)))
RL_EXPLORATION = min(1.0, max(0.0, _env_float("RL_EXPLORATION", 0.1)))
RL_FULL_ABOVE_R = _env_float("RL_FULL_ABOVE_R", 0.15)
RL_SKIP_BELOW_R = _env_float("RL_SKIP_BELOW_R", 0.0)
RL_PRIOR_STRENGTH = max(0.5, _env_float("RL_PRIOR_STRENGTH", 3))
REWARD_CLIP = (-3.0, 6.0)


def _bucket_vol(stop_pct):
    if stop_pct is None:
        return "na"
    if stop_pct < 1.0:
        return "low"
    if stop_pct < 2.5:
        return "mid"
    return "high"


def context_keys(market, setup, price=None, stop_distance=None):
    """Return (specific, parent) context keys; parent pools sparse contexts."""
    setup = setup or {}
    market = "futures" if "futures" in str(market).lower() else "spot"
    strategy = str(setup.get("strategy") or "unknown")
    direction = str(setup.get("direction") or "long")
    timeframe = str(setup.get("timeframe") or "na")
    stop_pct = None
    try:
        if price and stop_distance:
            stop_pct = float(stop_distance) / float(price) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        stop_pct = None
    parent = "{}|{}|{}".format(market, strategy, direction)
    specific = "{}|{}|vol:{}".format(parent, timeframe, _bucket_vol(stop_pct))
    return specific, parent


class BanditAgent:
    def __init__(self, db_path=":memory:", mode=None, min_samples=None, exploration=None,
                 rng=None, full_above=None, skip_below=None, prior_strength=None):
        self.db_path = db_path
        self.mode = (mode or RL_MODE)
        self.min_samples = int(min_samples if min_samples is not None else RL_MIN_SAMPLES)
        self.exploration = float(exploration if exploration is not None else RL_EXPLORATION)
        self.full_above = float(full_above if full_above is not None else RL_FULL_ABOVE_R)
        self.skip_below = float(skip_below if skip_below is not None else RL_SKIP_BELOW_R)
        self.prior_strength = float(prior_strength if prior_strength is not None else RL_PRIOR_STRENGTH)
        self.rng = rng or random.Random()
        self._lock = threading.Lock()
        self._mem = sqlite3.connect(":memory:", check_same_thread=False) if db_path == ":memory:" else None
        self._init()

    # -- storage ---------------------------------------------------------
    def _conn(self):
        if self._mem is not None:
            return self._mem
        conn = sqlite3.connect(self.db_path, timeout=10)
        return conn

    def _close(self, conn):
        if conn is not self._mem:
            conn.close()

    def _init(self):
        conn = self._conn()
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS rl_arms (
                context TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0,
                sum_r REAL NOT NULL DEFAULT 0, sum_r2 REAL NOT NULL DEFAULT 0,
                updated_at TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS rl_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, market TEXT, symbol TEXT,
                context TEXT, parent TEXT, mode TEXT, action TEXT, multiplier REAL,
                applied_multiplier REAL, sampled_r REAL, expected_r REAL, samples INTEGER,
                status TEXT, r_multiple REAL, closed_at TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rl_dec_sym ON rl_decisions(market, symbol, status)")
            conn.commit()
        finally:
            self._close(conn)

    def _arm(self, conn, context):
        row = conn.execute("SELECT n, sum_r, sum_r2 FROM rl_arms WHERE context=?", (context,)).fetchone()
        return (int(row[0]), float(row[1]), float(row[2])) if row else (0, 0.0, 0.0)

    # -- posterior -------------------------------------------------------
    def posterior(self, n, sum_r, sum_r2):
        """Normal posterior on mean R with a zero-mean prior of k0 pseudo-trades."""
        k0 = self.prior_strength
        mean = sum_r / (n + k0)
        if n >= 2:
            var = max(0.25, (sum_r2 - sum_r * sum_r / n) / (n - 1))
        else:
            var = 1.0
        sd = math.sqrt(var / (n + k0))
        return mean, sd

    def _choose(self, sampled):
        if sampled <= self.skip_below:
            return "skip"
        if sampled < self.full_above:
            return "half"
        return "full"

    def decide(self, market, symbol, setup, price=None, stop_distance=None):
        """Return a decision dict. applied_multiplier is always in [0, 1]."""
        neutral = {"mode": self.mode, "action": "full", "multiplier": 1.0,
                   "applied_multiplier": 1.0, "decision_id": None, "note_ar": ""}
        if self.mode == "off":
            return neutral
        try:
            specific, parent = context_keys(market, setup, price, stop_distance)
            with self._lock:
                conn = self._conn()
                try:
                    arm = self._arm(conn, specific)
                    used = specific
                    if arm[0] < self.min_samples:
                        parent_arm = self._arm(conn, parent)
                        if parent_arm[0] > arm[0]:
                            arm, used = parent_arm, parent
                    n = arm[0]
                    mean, sd = self.posterior(*arm)
                    sampled = self.rng.gauss(mean, sd)
                    action = self._choose(sampled)
                    explored = False
                    if action == "skip" and self.rng.random() < self.exploration:
                        action, explored = "half", True
                    multiplier = ACTIONS[action]
                    if self.mode == "live" and n >= self.min_samples:
                        applied = multiplier
                    else:
                        applied = 1.0
                    applied = min(1.0, max(0.0, float(applied)))
                    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    cur = conn.execute("""INSERT INTO rl_decisions
                        (created_at, market, symbol, context, parent, mode, action, multiplier,
                         applied_multiplier, sampled_r, expected_r, samples, status)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending')""",
                        (now, self._market(market), str(symbol), specific, parent, self.mode, action,
                         multiplier, applied, sampled, mean, n))
                    conn.commit()
                    decision_id = cur.lastrowid
                finally:
                    self._close(conn)
            gated = self.mode == "live" and n < self.min_samples
            note = "التعلم المعزز ({}): {} · R متوقع {:+.2f} · عينات {}{}{}".format(
                MODE_LABELS_AR.get(self.mode, self.mode), ACTION_LABELS_AR[action], mean, n,
                " · استكشاف" if explored else "",
                " · لم يُطبّق (عينات أقل من {})".format(self.min_samples) if gated else
                (" · للمراقبة فقط" if self.mode == "shadow" else ""))
            return {"mode": self.mode, "action": action, "multiplier": multiplier,
                    "applied_multiplier": applied, "decision_id": decision_id,
                    "expected_r": mean, "samples": n, "context": used, "explored": explored,
                    "note_ar": note}
        except Exception:
            return neutral

    @staticmethod
    def _market(market):
        return "futures" if "futures" in str(market).lower() else "spot"

    def mark_opened(self, decision_id):
        if not decision_id:
            return
        try:
            with self._lock:
                conn = self._conn()
                try:
                    conn.execute("UPDATE rl_decisions SET status='opened' WHERE id=?", (decision_id,))
                    conn.execute("""UPDATE rl_decisions SET status='superseded' WHERE status='opened'
                        AND id<>? AND market=(SELECT market FROM rl_decisions WHERE id=?)
                        AND symbol=(SELECT symbol FROM rl_decisions WHERE id=?)""",
                                 (decision_id, decision_id, decision_id))
                    conn.commit()
                finally:
                    self._close(conn)
        except Exception:
            pass

    def update_context(self, conn, context, reward):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute("""INSERT INTO rl_arms (context, n, sum_r, sum_r2, updated_at) VALUES (?,1,?,?,?)
            ON CONFLICT(context) DO UPDATE SET n=n+1, sum_r=sum_r+excluded.sum_r,
            sum_r2=sum_r2+excluded.sum_r2, updated_at=excluded.updated_at""",
                     (context, reward, reward * reward, now))

    def learn(self, specific, parent, r_multiple):
        """Online update of both context levels with a clipped realized R."""
        reward = min(REWARD_CLIP[1], max(REWARD_CLIP[0], float(r_multiple)))
        with self._lock:
            conn = self._conn()
            try:
                self.update_context(conn, specific, reward)
                self.update_context(conn, parent, reward)
                conn.commit()
            finally:
                self._close(conn)

    def record_outcome(self, market, symbol, r_multiple):
        """Called when a trade closes; updates the bandit for its opened decision."""
        if r_multiple is None:
            return False
        try:
            r_value = float(r_multiple)
            if not math.isfinite(r_value):
                return False
            with self._lock:
                conn = self._conn()
                try:
                    row = conn.execute("""SELECT id, context, parent FROM rl_decisions
                        WHERE market=? AND symbol=? AND status='opened' ORDER BY id DESC LIMIT 1""",
                                       (self._market(market), str(symbol))).fetchone()
                    if not row:
                        return False
                    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    conn.execute("UPDATE rl_decisions SET status='closed', r_multiple=?, closed_at=? WHERE id=?",
                                 (r_value, now, row[0]))
                    conn.commit()
                finally:
                    self._close(conn)
            self.learn(row[1], row[2], r_value)
            return True
        except Exception:
            return False

    def stats(self, limit=30):
        conn = self._conn()
        try:
            arms = []
            for context, n, sum_r, sum_r2, updated in conn.execute(
                    "SELECT context, n, sum_r, sum_r2, updated_at FROM rl_arms ORDER BY n DESC"):
                mean, sd = self.posterior(int(n), float(sum_r), float(sum_r2))
                probability_positive = 0.5 * (1 + math.erf(mean / (sd * math.sqrt(2)))) if sd > 0 else 0.5
                arms.append({"context": context, "level": "parent" if context.count("|") == 2 else "context",
                             "samples": int(n), "avg_r": round(float(sum_r) / n, 4) if n else None,
                             "expected_r": round(mean, 4), "uncertainty": round(sd, 4),
                             "prob_positive": round(probability_positive, 4),
                             "preferred_action": self._choose(mean),
                             "ready": int(n) >= self.min_samples, "updated_at": updated})
            closed = conn.execute("""SELECT COUNT(*), COALESCE(SUM(r_multiple),0),
                COALESCE(SUM(r_multiple*applied_multiplier),0), COALESCE(SUM(r_multiple*multiplier),0),
                COALESCE(SUM(CASE WHEN action='skip' THEN 1 ELSE 0 END),0)
                FROM rl_decisions WHERE status='closed'""").fetchone()
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM rl_decisions GROUP BY status").fetchall())
            recent = [dict(zip(("created_at", "market", "symbol", "context", "mode", "action", "multiplier",
                                "applied_multiplier", "expected_r", "samples", "status", "r_multiple"), row))
                      for row in conn.execute("""SELECT created_at, market, symbol, context, mode, action,
                        multiplier, applied_multiplier, expected_r, samples, status, r_multiple
                        FROM rl_decisions ORDER BY id DESC LIMIT ?""", (int(limit),))]
        finally:
            self._close(conn)
        return {
            "mode": self.mode, "mode_label": MODE_LABELS_AR.get(self.mode, self.mode),
            "min_samples": self.min_samples, "exploration": self.exploration,
            "thresholds": {"skip_below_r": self.skip_below, "full_above_r": self.full_above},
            "actions": ACTIONS, "arms": arms,
            "comparison": {"closed_trades": int(closed[0]), "baseline_r": round(float(closed[1]), 4),
                           "actual_r": round(float(closed[2]), 4), "rl_r": round(float(closed[3]), 4),
                           "rl_would_skip": int(closed[4])},
            "decision_counts": counts, "recent": recent,
        }


_AGENTS = {}


def get_agent(db_path):
    agent = _AGENTS.get(db_path)
    if agent is None:
        agent = _AGENTS[db_path] = BanditAgent(db_path)
    return agent
