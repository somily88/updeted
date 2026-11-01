import importlib, os, sys


def _reload(name, **env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        sys.modules.pop(name, None)
        return importlib.import_module(name)
    finally:
        for k, v in old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        sys.modules.pop(name, None)


def test_entry_mode_limit_alias():
    assert _reload("execution", ENTRY_ORDER_MODE="limit").ENTRY_ORDER_MODE == "limit_then_market"


def test_bounce_timeframes_used_in_scan():
    src = open(os.path.join(os.path.dirname(__file__), "strategy_engine.py")).read()
    assert "for timeframe in BOUNCE_TIMEFRAMES:" in src
    assert _reload("strategy_engine", BOUNCE_TIMEFRAMES="15m").BOUNCE_TIMEFRAMES == ("15m",)
