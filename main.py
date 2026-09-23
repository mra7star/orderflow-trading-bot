import os
import time
import hmac
import hashlib
import requests
import numpy as np
from datetime import datetime, timezone, timedelta

# === CONFIGURATION ===
# GitHub Secrets se secure tareeqay se uthayega
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

# MEXC Futures Demo API Credentials (Apni keys yahan dalein ya secrets use karein)
MEXC_API_KEY = "YAHAN_APNI_MEXC_API_KEY_DALIN"
MEXC_SECRET_KEY = "YAHAN_APNA_MEXC_SECRET_KEY_DALIN"
MEXC_BASE_URL = "https://contract.mexc.com"

HYPERLIQUID_API_URL = "https://api.hyperliquid.xyz/info"
BINANCE_FUTURES_URL = "https://fapi.binance.com/fapi/v1/trades"

# Side-Wise Alert Cooldown
ALERT_COOLDOWN = {}
COOLDOWN_TIME = 900  # 15 minutes per side per coin

# Taker Fees & Slippage Constants
TAKER_FEE = 0.00035
SLIPPAGE_EST = 0.0005


def send_discord_alert(msg):
    if not DISCORD_WEBHOOK_URL:
        print("Webhook Error: DISCORD_WEBHOOK_URL environment variable missing.")
        return
    payload = {"content": msg}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Webhook Error: {e}")


def place_mexc_demo_order(symbol, side, size_usdt, entry_price, sl_price, tp_price):
    """MEXC Futures Demo/Testnet par automatic trade execute karne ka function"""
    if "YAHAN" in MEXC_API_KEY:
        print("⚠️ MEXC API Keys not configured yet. Skipping auto-trade execution.")
        return False

    endpoint = "/api/v1/private/order/submit"
    timestamp = str(int(time.time() * 1000))
    mexc_symbol = f"{symbol}_USDT"
    contract_size = round(size_usdt / entry_price, 2)
    op_type = 1 if side == "LONG" else 3
    
    params = {
        "symbol": mexc_symbol,
        "price": entry_price,
        "vol": contract_size,
        "side": op_type,
        "type": 5,
        "openType": 1,
        "leverage": 10
    }
    
    query_string = f"accessKey={MEXC_API_KEY}&reqTime={timestamp}"
    signature = hmac.new(MEXC_SECRET_KEY.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
    
    headers = {
        "ApiKey": MEXC_API_KEY,
        "Request-Time": timestamp,
        "Signature": signature,
        "Content-Type": "application/json"
    }
    
    try:
        res = requests.post(MEXC_BASE_URL + endpoint, json=params, headers=headers, timeout=5).json()
        if res.get("success"):
            print(f"✅ MEXC Demo Trade Executed Successfully for {symbol} {side}!")
            return True
        else:
            print(f"❌ MEXC Order Failed: {res}")
            return False
    except Exception as e:
        print(f"❌ MEXC Connection Error: {e}")
        return False


def fetch_market_context():
    try:
        res = requests.post(HYPERLIQUID_API_URL, json={"type": "metaAndAssetCtxs"}, timeout=5)
        return res.json()
    except Exception:
        return None


def fetch_recent_trades_tape(coin):
    try:
        res = requests.post(HYPERLIQUID_API_URL, json={"type": "recentTrades", "coin": coin}, timeout=5).json()
        if not res or not isinstance(res, list):
            return 0.0, 0.0, False

        buyer_vol = 0.0
        seller_vol = 0.0

        for trade in res:
            sz = float(trade.get("sz", 0))
            px = float(trade.get("px", 0))
            side = trade.get("side", "")
            usd_val = sz * px

            if side == "B":
                buyer_vol += usd_val
            elif side == "A":
                seller_vol += usd_val

        total_vol = buyer_vol + seller_vol
        if total_vol == 0:
            return 0.0, 0.0, False

        net_delta = buyer_vol - seller_vol
        delta_pct = net_delta / total_vol

        footprint_imbalance = False
        if buyer_vol >= (3.0 * seller_vol) and buyer_vol > 15000:
            footprint_imbalance = True
        elif seller_vol >= (3.0 * buyer_vol) and seller_vol > 15000:
            footprint_imbalance = True

        return net_delta, delta_pct, footprint_imbalance
    except Exception:
        return 0.0, 0.0, False


def fetch_binance_cross_delta(symbol):
    try:
        params = {"symbol": symbol, "limit": 100}
        res = requests.get(BINANCE_FUTURES_URL, params=params, timeout=3).json()
        if not isinstance(res, list):
            return 0.0, True

        buyer_vol = 0.0
        seller_vol = 0.0

        for trade in res:
            qty = float(trade.get("qty", 0))
            price = float(trade.get("price", 0))
            is_buyer_maker = trade.get("isBuyerMaker", False)
            usd_val = qty * price

            if not is_buyer_maker:
                buyer_vol += usd_val
            else:
                seller_vol += usd_val

        total_vol = buyer_vol + seller_vol
        if total_vol == 0:
            return 0.0, False

        return buyer_vol - seller_vol, True
    except Exception:
        return 0.0, True


def fetch_anti_spoof_dom_depth(coin, samples=3, delay=0.15):
    bid_depths, ask_depths = [], []
    last_raw_bids, last_raw_asks = [], []

    for _ in range(samples):
        try:
            res = requests.post(HYPERLIQUID_API_URL, json={"type": "l2Book", "coin": coin}, timeout=5).json()
            bids = res.get("levels", [[], []])[0]
            asks = res.get("levels", [[], []])[1]
            last_raw_bids, last_raw_asks = bids, asks

            bid_vol = sum(float(b["sz"]) * float(b["px"]) for b in bids[:10])
            ask_vol = sum(float(a["sz"]) * float(a["px"]) for a in asks[:10])

            bid_depths.append(bid_vol)
            ask_depths.append(ask_vol)
            time.sleep(delay)
        except Exception:
            continue

    if not bid_depths or not ask_depths:
        return 1.0, 0.0, 0.0, [], []

    real_bid_depth = float(np.median(bid_depths))
    real_ask_depth = float(np.median(ask_depths))
    dom_ratio = real_bid_depth / real_ask_depth if real_ask_depth > 0 else 1.0
    return dom_ratio, real_bid_depth, real_ask_depth, last_raw_bids, last_raw_asks


def calculate_anti_sweep_sl(entry_price, side, atr, bid_levels, ask_levels):
    base_buffer = 1.8 * atr
    if side == "LONG":
        dense_bid_price = entry_price - base_buffer
        if bid_levels:
            max_bid = max(bid_levels[:5], key=lambda x: float(x["sz"]))
            wall_price = float(max_bid["px"])
            dense_bid_price = min(entry_price - (1.2 * atr), wall_price - (0.3 * atr))
        return dense_bid_price
    elif side == "SHORT":
        dense_ask_price = entry_price + base_buffer
        if ask_levels:
            max_ask = max(ask_levels[:5], key=lambda x: float(x["sz"]))
            wall_price = float(max_ask["px"])
            dense_ask_price = max(entry_price + (1.2 * atr), wall_price + (0.3 * atr))
        return dense_ask_price


def fetch_candles(coin, interval="5m", count=50):
    end_time = int(time.time() * 1000)
    start_time = end_time - (count * 5 * 60 * 1000)
    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_time, "endTime": end_time},
    }
    try:
        return requests.post(HYPERLIQUID_API_URL, json=payload, timeout=5).json()
    except Exception:
        return None


def calculate_atr(candles, period=14):
    highs = np.array([float(c["h"]) for c in candles])
    lows = np.array([float(c["l"]) for c in candles])
    closes = np.array([float(c["c"]) for c in candles])
    tr1 = highs[1:] - lows[1:]
    tr2 = np.abs(highs[1:] - closes[:-1])
    tr3 = np.abs(lows[1:] - closes[:-1])
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    return np.mean(tr[-period:])


def check_side_cooldown(coin, side, current_time):
    key = f"{coin}_{side}"
    if key in ALERT_COOLDOWN and (current_time - ALERT_COOLDOWN[key]) < COOLDOWN_TIME:
        return False
    return True


def analyze_v8_orderflow_engine():
    current_time = time.time()
    ctx_data = fetch_market_context()
    if not ctx_data:
        return

    universe = ctx_data[0]["universe"]
    ctxs = ctx_data[1]

    for idx, coin_info in enumerate(universe):
        coin = coin_info["name"]
        if coin == "BTC":
            continue

        ctx = ctxs[idx]
        day_vol = float(ctx.get("dayNtlVlm", 0))
        open_interest = float(ctx.get("openInterest", 0))
        funding = float(ctx.get("funding", 0))

        if day_vol < 15000000:
            continue

        net_delta, delta_pct, footprint_imbalance = fetch_recent_trades_tape(coin)
        binance_pair = f"{coin}USDT"
        binance_delta, _ = fetch_binance_cross_delta(binance_pair)
        dom_ratio, _, _, raw_bids, raw_asks = fetch_anti_spoof_dom_depth(coin, samples=3, delay=0.1)

        candles = fetch_candles(coin, "5m", count=50)
        if not candles or len(candles) < 30:
            continue

        current_price = float(candles[-1]["c"])
        atr = calculate_atr(candles)

        allow_long = funding < 0.0005
        allow_short = funding > -0.0005

        pkt_zone = timezone(timedelta(hours=5))
        pkt_time_str = datetime.now(pkt_zone).strftime("%d-%b-%Y %I:%M:%S %p")

        # 🟢 LONG SETUP
        if allow_long and footprint_imbalance and delta_pct > 0.12 and dom_ratio > 1.35 and binance_delta > 0:
            if check_side_cooldown(coin, "LONG", current_time):
                sl = calculate_anti_sweep_sl(current_price, "LONG", atr, raw_bids, raw_asks)
                risk = current_price - sl
                tp = current_price + (2.5 * risk)

                cost_pct = (2 * TAKER_FEE) + (2 * SLIPPAGE_EST)
                net_rr = ((2.5 * risk / current_price) - cost_pct) / (risk / current_price)

                if net_rr >= 1.8:
                    ALERT_COOLDOWN[f"{coin}_LONG"] = current_time
                    
                    alert = (
                        f"⚡ **LEVEL-3 ORDER FLOW ULTRA LONG (MEXC AUTO)** ⚡\n\n"
                        f"🕒 **Time (PKT):** `{pkt_time_str}`\n"
                        f"🪙 **Coin:** `{coin}`\n"
                        f"💵 **Entry Price:** `${current_price:.4f}`\n"
                        f"🛑 **Anti-Sweep SL:** `${sl:.4f}` | 🎯 **TP:** `${tp:.4f}`"
                    )
                    send_discord_alert(alert)
                    place_mexc_demo_order(coin, "LONG", 50.0, current_price, sl, tp)

        # 🔴 SHORT SETUP
        elif allow_short and footprint_imbalance and delta_pct < -0.12 and dom_ratio < 0.75 and binance_delta < 0:
            if check_side_cooldown(coin, "SHORT", current_time):
                sl = calculate_anti_sweep_sl(current_price, "SHORT", atr, raw_bids, raw_asks)
                risk = sl - current_price
                tp = current_price - (2.5 * risk)

                cost_pct = (2 * TAKER_FEE) + (2 * SLIPPAGE_EST)
                net_rr = ((2.5 * risk / current_price) - cost_pct) / (risk / current_price)

                if net_rr >= 1.8:
                    ALERT_COOLDOWN[f"{coin}_SHORT"] = current_time
                    
                    alert = (
                        f"⚡ **LEVEL-3 ORDER FLOW ULTRA SHORT (MEXC AUTO)** ⚡\n\n"
                        f"🕒 **Time (PKT):** `{pkt_time_str}`\n"
                        f"🪙 **Coin:** `{coin}`\n"
                        f"💵 **Entry Price:** `${current_price:.4f}`\n"
                        f"🛑 **Anti-Sweep SL:** `${sl:.4f}` | 🎯 **TP:** `${tp:.4f}`"
                    )
                    send_discord_alert(alert)
                    place_mexc_demo_order(coin, "SHORT", 50.0, current_price, sl, tp)


if __name__ == "__main__":
    print("🚀 V8 Level-3 Order Flow Engine Running...")
    try:
        analyze_v8_orderflow_engine()
        print("✅ Scan Cycle Complete!")
    except Exception as e:
        print(f"Error in execution: {e}")
