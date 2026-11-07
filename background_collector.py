
import os
import math
from datetime import datetime, timezone

import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
COINBASE = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

session = requests.Session()
session.headers.update({"User-Agent": "KalshiBTCCollector/1.0"})

def kalshi_get(path, params=None):
    response = session.get(
        KALSHI + path,
        params=params,
        timeout=20
    )
    response.raise_for_status()
    return response.json()

def database_request(method, params=None, data=None):
        headers = {
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    response = session.request(
        method,
        SUPABASE_URL + "/rest/v1/btc_predictions",
        headers=headers,
        params=params,
        json=data,
        timeout=20,
    )
    if not response.ok:
    print("Supabase error:", response.status_code, response.text[:1000])
response.raise_for_status()
    return response.json() if response.content else []

def get_spot():
    response = session.get(COINBASE, timeout=20)
    response.raise_for_status()
    return float(response.json()["data"]["amount"])

def get_target(market):
    for field in ("floor_strike", "strike_price"):
        value = market.get(field)
        if value is not None:
            try:
                target = float(value)
                if target > 1000:
                    return target
            except (ValueError, TypeError):
                pass
    return None

def get_close_time(market):
    value = market.get("close_time")
    if not value:
        return None
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )

def probability_up(spot, target, seconds_left):
    # Illustrative model, NOT a validated trading forecast.
    # Assumes 0.10% BTC volatility per sqrt(hour).
    hours = max(seconds_left, 1) / 3600
    sigma = spot * 0.001 * math.sqrt(hours)
    z = (spot - target) / max(sigma, 0.01)
    p = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return max(0.001, min(0.999, p))

def record_predictions():
    markets = kalshi_get(
        "/markets",
        {"series_ticker": "KXBTC15M",
         "status": "open", "limit": 100}
    ).get("markets", [])

    spot = get_spot()
    now = datetime.now(timezone.utc)

    for market in markets:
        ticker = market.get("ticker")
        target = get_target(market)
        close = get_close_time(market)

        if not ticker or target is None or close is None:
            continue

        seconds_left = int((close - now).total_seconds())

        # Lock forecasts in the same 5-10 minute window
        # used by the existing dashboard.
        if not 300 <= seconds_left <= 600:
            continue

        existing = database_request(
            "GET",
            {"select": "ticker", "ticker": "eq." + ticker}
        )
        if existing:
            continue

        p_up = probability_up(spot, target, seconds_left)
        direction = "UP" if p_up >= 0.5 else "DOWN"

        row = {
            "ticker": ticker,
            "direction": direction,
            "p_up": p_up,
            "seconds_left": seconds_left,
            "target_price": target,
            "spot_price": spot,
            "predicted_at": now.isoformat(),
        }

        database_request("POST", data=[row])
        print("Recorded:", ticker, direction, round(p_up, 4))

def settle_predictions():
    rows = database_request(
        "GET",
        {
            "select": "ticker,p_up,direction",
            "actual_result": "is.null",
            "limit": "200",
        },
    )

    for row in rows:
        try:
            market = kalshi_get(
                "/markets/" + row["ticker"]
            )["market"]

            result = str(market.get("result") or "").lower()
            if result not in ("yes", "no"):
                continue

            # KXBTC15M YES means UP; NO means DOWN.
            actual = "UP" if result == "yes" else "DOWN"
            outcome = 1 if actual == "UP" else 0
            p_up = float(row["p_up"])

            update = {
                "actual_result": actual,
                "correct": row["direction"] == actual,
                "brier_score": (p_up - outcome) ** 2,
                "settled_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            }

            database_request(
                "PATCH",
                {"ticker": "eq." + row["ticker"]},
                update,
            )
            print("Settled:", row["ticker"], actual)

        except Exception as exc:
            print("Settlement check failed:", exc)

if __name__ == "__main__":
    settle_predictions()
    record_predictions()
    print("Collector run completed.")
