#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord → Altrady Forwarder (Auto DCA + TP + Trailing)
Funktioniert mit Andre Outberg / AO Team Signalen im Format:

PHB SHORT Signal
PHB on ByBit (Conditional Trigger)
Enter on Trigger: $0.4565
TP1: $0.4526
TP2: $0.4490
TP3: $0.4380
DCA #1: $0.4793
DCA #2: $0.5250
DCA #3: $0.6163
"""

import os, re, sys, time, json, traceback
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# =========================
# ENVIRONMENT VARS
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

ALTRADY_WEBHOOK_URL = os.getenv("ALTRADY_WEBHOOK_URL", "").strip()
ALTRADY_API_KEY = os.getenv("ALTRADY_API_KEY", "").strip()
ALTRADY_API_SECRET = os.getenv("ALTRADY_API_SECRET", "").strip()
ALTRADY_EXCHANGE = os.getenv("ALTRADY_EXCHANGE", "BYBI").strip()
QUOTE = os.getenv("QUOTE", "USDT").strip().upper()
FIXED_LEVERAGE = int(os.getenv("FIXED_LEVERAGE", "25"))

# Take Profit Split + Runner
TP1_PCT = float(os.getenv("TP1_PCT", "30"))
TP2_PCT = float(os.getenv("TP2_PCT", "30"))
TP3_PCT = float(os.getenv("TP3_PCT", "30"))
RUNNER_PCT = float(os.getenv("RUNNER_PCT", "10"))

# Trailing Stop für Runner
TRAILING_PERCENTAGE = float(os.getenv("TRAILING_PERCENTAGE", "3.0"))
TRAILING_DISTANCE = float(os.getenv("TRAILING_DISTANCE", "0.5"))

# Harter Stop optional (relativ zu DCA3)
USE_HARD_SL = os.getenv("USE_HARD_SL", "off").lower() == "on"
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "5.0"))

# DCA-Größen
DCA1_QTY_PCT = float(os.getenv("DCA1_QTY_PCT", "150"))
DCA2_QTY_PCT = float(os.getenv("DCA2_QTY_PCT", "225"))
DCA3_QTY_PCT = float(os.getenv("DCA3_QTY_PCT", "340"))

# Ablaufzeit Limit-Orders
ENTRY_EXPIRATION_MIN = int(os.getenv("ENTRY_EXPIRATION_MIN", "60"))

# Polling
POLL_BASE_SECONDS = int(os.getenv("POLL_BASE_SECONDS", "60"))
POLL_OFFSET_SECONDS = int(os.getenv("POLL_OFFSET_SECONDS", "3"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

if not DISCORD_TOKEN or not CHANNEL_ID or not ALTRADY_WEBHOOK_URL:
    print("❌ Bitte ENV setzen: DISCORD_TOKEN, CHANNEL_ID, ALTRADY_WEBHOOK_URL (+ Keys).")
    sys.exit(1)

HEADERS = {
    "Authorization": DISCORD_TOKEN,
    "User-Agent": "DiscordToAltrady-DCA/1.1"
}

# =========================
# HELPERS
# =========================
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_id": None}

def save_state(st: dict):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    tmp.replace(STATE_FILE)

def fetch_latest_message(channel_id: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    r = requests.get(url, headers=HEADERS, params={"limit": 1}, timeout=15)
    if r.status_code == 429:
        retry = float(r.json().get("retry_after", 5))
        time.sleep(retry + 0.5)
        r = requests.get(url, headers=HEADERS, params={"limit": 1}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None

# =========================
# PARSER
# =========================
PAIR_LINE = re.compile(r"\b([A-Z0-9]{2,})\s+(LONG|SHORT)\s+Signal\b", re.I)
ENTER_LINE = re.compile(r"Enter\s+on\s+Trigger:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
TP_LINES = [re.compile(fr"TP{i}:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I) for i in range(1, 4)]
DCA_LINES = [re.compile(fr"DCA\s*#?{i}:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I) for i in range(1, 4)]

def extract_all_text(msg: dict) -> str:
    """Sammelt content + alle Embeds vollständig."""
    parts = []
    if msg.get("content"): parts.append(msg["content"])
    for e in msg.get("embeds", []):
        if not isinstance(e, dict): continue
        for k in ("title", "description"): 
            if e.get(k): parts.append(e[k])
        for fld in e.get("fields", []) or []:
            if fld.get("name"): parts.append(fld["name"])
            if fld.get("value"): parts.append(fld["value"])
        f = e.get("footer") or {}
        if f.get("text"): parts.append(f["text"])
    raw = "\n".join(parts).replace("\r", "")
    return raw.strip()

def parse_signal(text: str):
    t = text.replace("`", "")
    m_pair = PAIR_LINE.search(t)
    if not m_pair:
        return None
    base = m_pair.group(1).upper()
    side = "long" if m_pair.group(2).upper() == "LONG" else "short"

    m_e = ENTER_LINE.search(t)
    if not m_e:
        return None
    entry = float(m_e.group(1))

    tps = [float(m.search(t).group(1)) if m.search(t) else None for m in TP_LINES]
    dcas = [float(m.search(t).group(1)) if m.search(t) else None for m in DCA_LINES]
    if None in tps or None in dcas:
        return None

    tp1, tp2, tp3 = tps
    d1, d2, d3 = dcas
    if side == "long":
        ok = (tp1 > entry and tp2 > entry and tp3 > entry and d1 < entry and d2 < entry and d3 < entry)
    else:
        ok = (tp1 < entry and tp2 < entry and tp3 < entry and d1 > entry and d2 > entry and d3 > entry)
    if not ok:
        return None
    return {"base": base, "side": side, "entry": entry,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "dca1": d1, "dca2": d2, "dca3": d3}

# =========================
# PAYLOAD BUILDER
# =========================
def pct_dist(entry, price): return abs((price - entry) / entry) * 100.0

def build_payload(sig):
    entry, tp1, tp2, tp3 = sig["entry"], sig["tp1"], sig["tp2"], sig["tp3"]
    d1, d2, d3 = sig["dca1"], sig["dca2"], sig["dca3"]
    symbol = f"{ALTRADY_EXCHANGE}_{QUOTE}_{sig['base']}"
    tp1_pct, tp2_pct, tp3_pct = map(lambda p: pct_dist(entry, p), (tp1, tp2, tp3))
    dca1_pct, dca2_pct, dca3_pct = map(lambda p: pct_dist(entry, p), (d1, d2, d3))

    stop_percentage = None
    if USE_HARD_SL:
        sl_price = d3 * (1 + SL_BUFFER_PCT / 100.0) if sig["side"] == "short" else d3 * (1 - SL_BUFFER_PCT / 100.0)
        stop_percentage = pct_dist(entry, sl_price)

    payload = {
        "action": "open",
        "api_key": ALTRADY_API_KEY,
        "api_secret": ALTRADY_API_SECRET,
        "exchange": ALTRADY_EXCHANGE,
        "symbol": symbol,
        "side": sig["side"],
        "order_type": "limit",
        "signal_price": float(f"{entry:.10f}"),
        "leverage": FIXED_LEVERAGE,
        "dca_orders": [
            {"price_percentage": dca1_pct, "quantity_percentage": DCA1_QTY_PCT},
            {"price_percentage": dca2_pct, "quantity_percentage": DCA2_QTY_PCT},
            {"price_percentage": dca3_pct, "quantity_percentage": DCA3_QTY_PCT},
        ],
        "take_profit": [
            {"price_percentage": tp1_pct, "position_percentage": TP1_PCT},
            {"price_percentage": tp2_pct, "position_percentage": TP2_PCT},
            {"price_percentage": tp3_pct, "position_percentage": TP3_PCT},
        ],
        "stop_loss": {
            **({"stop_percentage": stop_percentage} if stop_percentage else {}),
            "protection_type": "PRICE",
            "trailing_percentage": TRAILING_PERCENTAGE,
            "trailing_distance": TRAILING_DISTANCE,
        },
        "entry_expiration": {"time": ENTRY_EXPIRATION_MIN}
    }
    return payload

# =========================
# SENDER
# =========================
def post_to_altrady(payload):
    for attempt in range(3):
        try:
            r = requests.post(ALTRADY_WEBHOOK_URL, json=payload, timeout=20)
            if r.status_code == 429:
                delay = float(r.json().get("retry_after", 2.0))
                time.sleep(delay + 0.25)
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))

# =========================
# MAIN LOOP
# =========================
def main():
    print(f"➡️ Verbunden mit Discord-Kanal {CHANNEL_ID} / Exchange {ALTRADY_EXCHANGE}")
    state = load_state()
    last_id = state.get("last_id")
    while True:
        try:
            msg = fetch_latest_message(CHANNEL_ID)
            if not msg:
                print("Kanal leer.")
            else:
                mid = msg["id"]
                if last_id is None or int(mid) > int(last_id):
                    raw = extract_all_text(msg)
                    parsed = parse_signal(raw)
                    if not parsed:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Kein gültiges Signal erkannt.")
                        print("[RAW PREVIEW]", raw[:300].replace("\n", " ⏎ "))
                    else:
                        print(f"[PARSED] {parsed}")
                        payload = build_payload(parsed)
                        _ = post_to_altrady(payload)
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Gesendet an Altrady: {parsed['base']} {parsed['side']} @ {parsed['entry']}")
                    last_id = mid
                    state["last_id"] = last_id
                    save_state(state)
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Keine neuere Nachricht.")
        except Exception:
            print("[ERROR]")
            traceback.print_exc()
        time.sleep(POLL_BASE_SECONDS)

if __name__ == "__main__":
    main()
