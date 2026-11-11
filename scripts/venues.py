"""Order payloads and request signing for Kalshi and Polymarket US. Nothing here sends anything unless `send()` is called
AND trader.live_allowed() passes; in dry-run the payloads are only built and logged.

Both venues use ONE book per market (the YES side):
  * Kalshi V2   POST /trade-api/v2/portfolio/events/orders   side "bid" = buy YES, "ask" = sell YES (= buy NO at 1 - price)
  * Polymarket US  POST https://api.polymarket.us/v1/orders   intent BUY_LONG = buy YES, BUY_SHORT = buy NO (sell YES)
Docs read 2026-11-11: docs.kalshi.com (create-order-v2, quick_start_authenticated_requests), docs.polymarket.us
(api-reference/orders/create-order, api-reference/authentication, concepts/orders).

Keys come only from environment variables (GitHub secrets later); never from files in this repo:
  KALSHI_KEY_ID, KALSHI_PRIVATE_KEY (PEM text)     POLYMARKET_KEY_ID, POLYMARKET_SECRET_KEY (base64)
"""
import base64
import json
import os
import time
import urllib.request
import uuid

KALSHI_API = "https://api.elections.kalshi.com"
KALSHI_DEMO_API = "https://external-api.demo.kalshi.co"  # fake-money exchange for testing live code
KALSHI_ORDER_PATH = "/trade-api/v2/portfolio/events/orders"
PMUS_API = "https://api.polymarket.us"
PMUS_ORDER_PATH = "/v1/orders"
PMUS_PREVIEW_PATH = "/v1/order/preview"

# Polymarket US docs say an order's price "always refers to the YES side", so buying NO at 0.40 is sent as 0.60.
# Not yet confirmed with a real preview call. trader.live_allowed() refuses live trading until this is True; flip it
# only after POST /v1/order/preview on a tiny BUY_SHORT order shows the expected price.
PMUS_SHORT_PRICE_IS_YES_PRICE_VERIFIED = False


def _px(p):
    return f"{p:.4f}"


def leg_raw_side(norm_side, flipped):
    """Our rows are normalized ("YES = X > line"); a flipped row's YES is the venue's NO."""
    return norm_side if not int(flipped or 0) else ("no" if norm_side == "yes" else "yes")


def kalshi_order(ticker, raw_side, price, contracts, resting=False, expires_ts=None):
    """Buy `contracts` of `raw_side` ("yes"/"no") at `price` (the price of that side, in dollars).
    Taker legs: immediate_or_cancel, so nothing is left behind. Resting legs: post_only, good_till_canceled with an
    expiration so a stale quote can't sit on the book."""
    body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": "bid" if raw_side == "yes" else "ask",
        "count": f"{contracts:.2f}",
        "price": _px(price if raw_side == "yes" else 1 - price),  # V2 quotes everything as the YES price
        "time_in_force": "good_till_canceled" if resting else "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": bool(resting),
        "cancel_order_on_pause": True,
        "reduce_only": False,
    }
    if resting and expires_ts:
        body["expiration_time"] = int(expires_ts)
    return {"venue": "kalshi", "method": "POST", "path": KALSHI_ORDER_PATH, "body": body}


def pmus_order(slug, raw_side, price, contracts, resting=False):
    """Buy `contracts` of `raw_side` at `price` (price of that side). BUY_SHORT is sent at the YES price (see flag above)."""
    body = {
        "marketSlug": slug,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": _px(price if raw_side == "yes" else 1 - price), "currency": "USD"},
        "quantity": float(contracts),
        "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL" if resting else "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
        "intent": "ORDER_INTENT_BUY_LONG" if raw_side == "yes" else "ORDER_INTENT_BUY_SHORT",
        "participateDontInitiate": bool(resting),
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    return {"venue": "polymarket_us", "method": "POST", "path": PMUS_ORDER_PATH, "body": body}


def build_leg(venue, market_id, norm_side, flipped, price, contracts, resting=False, expires_ts=None):
    raw = leg_raw_side(norm_side, flipped)
    if venue == "kalshi":
        return kalshi_order(market_id, raw, price, contracts, resting, expires_ts)
    return pmus_order(market_id, raw, price, contracts, resting)


# ------------------------------------------------------------------------------------------------- signing (live only)
def _kalshi_headers(method, path):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(os.environ["KALSHI_PRIVATE_KEY"].encode(), password=None)
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method}{path.split('?')[0]}".encode()
    if isinstance(key, Ed25519PrivateKey):
        sig = key.sign(msg)
    else:
        sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": os.environ["KALSHI_KEY_ID"], "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(), "Content-Type": "application/json"}


def _pmus_headers(method, path):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ["POLYMARKET_SECRET_KEY"])[:32])
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(key.sign(f"{ts}{method}{path}".encode())).decode()
    return {"X-PM-Access-Key": os.environ["POLYMARKET_KEY_ID"], "X-PM-Timestamp": ts, "X-PM-Signature": sig,
            "Content-Type": "application/json"}


def keys_present():
    need = ["KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY", "POLYMARKET_KEY_ID", "POLYMARKET_SECRET_KEY"]
    return [k for k in need if not os.environ.get(k)]


def send(order, allowed, demo=False):
    """Send one order. `allowed` must be the result of trader.live_allowed(); refuses otherwise."""
    if allowed is not True:
        raise PermissionError(f"live trading blocked: {allowed}")
    base = (KALSHI_DEMO_API if demo else KALSHI_API) if order["venue"] == "kalshi" else PMUS_API
    hdr = (_kalshi_headers if order["venue"] == "kalshi" else _pmus_headers)(order["method"], order["path"])
    req = urllib.request.Request(base + order["path"], data=json.dumps(order["body"]).encode(), headers=hdr,
                                 method=order["method"])
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)
