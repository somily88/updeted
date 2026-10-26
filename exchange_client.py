"""Construction of Binance Spot CCXT clients for supported environments."""
import ccxt


def make_binance_exchange(api_key, secret_key, environment="demo"):
    client = ccxt.binance({
        "apiKey": api_key,
        "secret": secret_key,
        "enableRateLimit": True,
        "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    })
    environment = (environment or "demo").strip().lower()
    if environment == "demo":
        if not hasattr(client, "enable_demo_trading"):
            raise RuntimeError("Installed ccxt does not support Binance Demo")
        client.enable_demo_trading(True)
    elif environment == "testnet":
        client.set_sandbox_mode(True)
    elif environment != "live":
        raise ValueError("Unknown Binance environment: {}".format(environment))
    return client
