#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, traceback, html, random
from datetime import datetime
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv()

# =========================
# ENVs
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID    = os.getenv("CHANNEL_ID", "").strip()

ALTRADY_WEBHOOK_URL = os.getenv("ALTRADY_WEBHOOK_URL", "").strip()
ALTRADY_API_KEY     = os.getenv("ALTRADY_API_KEY", "").strip()
ALTRADY_API_SECRET  = os.getenv("ALTRADY_API_SECRET", "").strip()
ALTRADY_EXCHANGE    = os.getenv("ALTRADY_EXCHANGE", "BYBI").strip()  # BYBIF für Bybit Futures, MEXC, ...
QUOTE               = os.getenv("QUOTE", "USDT").strip().upper()

FIXED_LEVERAGE      = int(os.getenv("FIXED_LEVERAGE", "25"))
TP1_PCT             = float(os.getenv("TP1_PCT", "30"))
TP2_PCT             = float(os.getenv("TP2_PCT", "30"))
TP3_PCT             = float(os.getenv("TP3_PCT", "30"))
RUNNER_PCT          = float(os.getenv("RUNNER_PCT", "10"))

TRAILING_PERCENTAGE = float(os.getenv("TRAILING_PERCENTAGE", "3.0"))
TRAILING_DISTANCE   = float(os.getenv("TRAILING_DISTANCE", "0.5"))

USE_HARD_SL         = os.getenv("USE_HARD_SL", "off").lower() == "on"
SL_BUFFER_PCT       = float(os.getenv("SL_BUFFER_PCT", "5.0"))

DCA1_QTY_PCT        = float(os.getenv("DCA1_QTY_PCT", "150"))
DCA2_QTY_PCT        = float(os.getenv("DCA2_QTY_PCT", "225"))
DCA3_QTY_PCT        = float(os.getenv("DCA3_QTY_PCT", "340"))

ENTRY_EXPIRATION_MIN= int(os.getenv("ENTRY_EXPIRATION_MIN", "60"))

POLL_BASE_SECONDS   = int(os.getenv("POLL_BASE_SECONDS", "60"))
POLL_OFFSET_SECONDS = int(os.getenv("POLL_OFFSET_SECONDS", "3"))
POLL_JITTER_MAX     = int(os.getenv("POLL_JITTER_MAX", "7"))

STATE_FILE          = Path(os.getenv("STATE_FILE", "state.json"))

# =========================
# Sanity
# =========================
if not DISCORD_TOKEN or not CHANNEL_ID or not ALTRADY_WEBHOOK_URL:
    print("Bitte ENV setzen: DISCORD_TOKEN, CHANNEL_ID, ALTRADY_WEBHOOK_URL (+ Keys).")
    sys.exit(1)

HEADERS = {
    "Authorization": DISCORD_TOKEN,   # User-Session oder Bot-Token
    "User-Agent": "DiscordToAltrady-DCA/1.4"
}

# =========================
# Utils
# =========================
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except:
            pass
    return {"last_id": None}

def save_state(st: dict):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    tmp.replace(STATE_FILE)

def sleep_until_next_tick():
    now = time.time()
    period_start = (now // POLL_BASE_SECONDS) * POLL_BASE_SECONDS
    next_tick = period_start + POLL_BASE_SECONDS + POLL_OFFSET_SECONDS
    if now < period_start + POLL_OFFSET_SECONDS:
        next_tick = period_start + POLL_OFFSET_SECONDS
    jitter = random.uniform(0, max(0, POLL_JITTER_MAX))
    time.sleep(max(0, next_tick - now + jitter))

def fetch_latest_message(channel_id: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    r = requests.get(url, headers=HEADERS, params={"limit":1}, timeout=15)
    if r.status_code == 429:
        retry = 5
        try:
            retry = float(r.json().get("retry_after", 5))
        except:
            pass
        time.sleep(retry + 0.5)
        r = requests.get(url, headers=HEADERS, params={"limit":1}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data[0] if data else None

# =========================
# Cleaning (Markdown/HTML -> Plain)
# =========================
MD_LINK   = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
MD_MARK   = re.compile(r"[*_`~]+")
MULTI_WS  = re.compile(r"[ \t\u00A0]+")

def clean_markdown(s: str) -> str:
    if not s: return ""
    s = s.replace("\r", "")
    s = html.unescape(s)
    s = MD_LINK.sub(r"\1", s)     # [text](url) -> text
    s = MD_MARK.sub("", s)        # *, _, `, ~ entfernen
    s = MULTI_WS.sub(" ", s)      # mehrfachen Whitespace normalisieren
    # Emojis stören nicht, wir lassen sie drin.
    s = "\n".join(line.strip() for line in s.split("\n"))
    return s.strip()

# =========================
# Regex (tolerant, alle Varianten)
# =========================
# A) Altes Format: "DIA SHORT Signal"
PAIR_OLD   = re.compile(r"(^|\n)\s*([A-Z0-9]+)\s+(LONG|SHORT)\s+Signal\s*(\n|$)", re.I)

# B) Live Trade Signal: "PUFFER/USDT SHORT …"
PAIR_SLASH = re.compile(r"(^|\n)\s*([A-Z0-9]+)\s*/\s*(USDT|USDC)\s+(LONG|SHORT)\b", re.I)

# C) APP-Format: "Coin: SD" + "Direction: SHORT"
COIN_LINE  = re.compile(r"(^|\n)\s*Coin\s*:\s*([A-Z0-9]+)\b", re.I)
DIR_LINE   = re.compile(r"(^|\n)\s*Direction\s*:\s*(LONG|SHORT)\b", re.I)

# Entry: mehrere Varianten
ENTER_TRIGGER  = re.compile(r"Enter\s+on\s+Trigger\s*:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
ENTRY_COLON    = re.compile(r"(^|\n)\s*Entry\s*:\s*\$?\s*([0-9]*\.?[0-9]+)\b", re.I)
ENTRY_BLOCK    = re.compile(r"(^|\n)\s*ENTRY\s*$\s*^\s*\$?\s*([0-9]*\.?[0-9]+)\b", re.I | re.M)

# TPs (mit oder ohne Status-Emojis)
TP1_LINE       = re.compile(r"TP\s*1\s*:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
TP2_LINE       = re.compile(r"TP\s*2\s*:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
TP3_LINE       = re.compile(r"TP\s*3\s*:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)

# DCA (mit # optional, Emojis egal)
DCA1_LINE      = re.compile(r"DCA\s*#?\s*1\s*:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
DCA2_LINE      = re.compile(r"DCA\s*#?\s*2\s*:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
DCA3_LINE      = re.compile(r"DCA\s*#?\s*3\s*:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)

def find_base_side(raw: str):
    # Priorität: Slash-Format, dann alt, dann APP-Format
    m = PAIR_SLASH.search(raw)
    if m:
        base = m.group(2).upper()
        side = "long" if m.group(4).upper()=="LONG" else "short"
        return base, side
    m = PAIR_OLD.search(raw)
    if m:
        base = m.group(2).upper()
        side = "long" if m.group(3).upper()=="LONG" else "short"
        return base, side
    m_coin = COIN_LINE.search(raw)
    m_dir  = DIR_LINE.search(raw)
    if m_coin and m_dir:
        base = m_coin.group(2).upper()
        side = "long" if m_dir.group(2).upper()=="LONG" else "short"
        return base, side
    return None, None

def find_entry(raw: str):
    m = ENTER_TRIGGER.search(raw)
    if m:
        return float(m.group(1))
    m = ENTRY_COLON.search(raw)
    if m:
        return float(m.group(2))
    m = ENTRY_BLOCK.search(raw)
    if m:
        return float(m.group(2))
    # Fallback: in manchen „Live Trade Signal“ Blöcken steht "ENTRY" und direkt darunter die Zahl
    # Das deckt ENTRY_BLOCK ab; mehr brauchen wir i.d.R. nicht.
    return None

def parse_signal_from_message(msg: dict):
    parts = [msg.get("content") or ""]
    embeds = msg.get("embeds") or []
    if embeds and isinstance(embeds, list):
        e0 = embeds[0] or {}
        desc = e0.get("description") or ""
        if desc: parts.append(desc)

    raw = clean_markdown("\n".join([p for p in parts if p]))
    print("[RAW PREVIEW CLEANED]")
    print("\n".join(raw.split("\n")[:60]))

    base, side = find_base_side(raw)
    if not base or not side:
        return None

    entry = find_entry(raw)
    mt1 = TP1_LINE.search(raw)
    mt2 = TP2_LINE.search(raw)
    mt3 = TP3_LINE.search(raw)
    md1 = DCA1_LINE.search(raw)
    md2 = DCA2_LINE.search(raw)
    md3 = DCA3_LINE.search(raw)

    if entry is None or not all([mt1, mt2, mt3, md1, md2, md3]):
        return None

    tp1 = float(mt1.group(1)); tp2 = float(mt2.group(1)); tp3 = float(mt3.group(1))
    d1  = float(md1.group(1)); d2  = float(md2.group(1)); d3  = float(md3.group(1))

    # Plausibilität
    if side == "long":
        ok = (tp1>entry and tp2>entry and tp3>entry and d1<entry and d2<entry and d3<entry)
    else:
        ok = (tp1<entry and tp2<entry and tp3<entry and d1>entry and d2>entry and d3>entry)
    if not ok:
        print("⚠️ Plausibility failed (side/levels mismatch).")
        return None

    return {"base":base,"side":side,"entry":entry,"tp1":tp1,"tp2":tp2,"tp3":tp3,"dca1":d1,"dca2":d2,"dca3":d3}

# =========================
# Payload Builder
# =========================
def pct_dist(entry: float, price: float) -> float:
    return abs((price - entry) / entry) * 100.0

def build_altrady_open_payload(sig: dict) -> dict:
    base, side, entry = sig["base"], sig["side"], sig["entry"]
    tp1, tp2, tp3     = sig["tp1"], sig["tp2"], sig["tp3"]
    d1, d2, d3        = sig["dca1"], sig["dca2"], sig["dca3"]

    symbol = f"{ALTRADY_EXCHANGE}_{QUOTE}_{base}"

    tp1_pct  = pct_dist(entry, tp1)
    tp2_pct  = pct_dist(entry, tp2)
    tp3_pct  = pct_dist(entry, tp3)
    dca1_pct = pct_dist(entry, d1)
    dca2_pct = pct_dist(entry, d2)
    dca3_pct = pct_dist(entry, d3)

    stop_percentage = None
    if USE_HARD_SL:
        sl_price = d3 * (1.0 + SL_BUFFER_PCT/100.0) if side=="short" else d3 * (1.0 - SL_BUFFER_PCT/100.0)
        stop_percentage = pct_dist(entry, sl_price)

    payload = {
        "action": "open",
        "api_key": ALTRADY_API_KEY,
        "api_secret": ALTRADY_API_SECRET,
        "exchange": ALTRADY_EXCHANGE,
        "symbol": symbol,
        "side": side,                    # "long" | "short"
        "order_type": "limit",
        "signal_price": float(f"{entry:.10f}"),
        "leverage": FIXED_LEVERAGE,

        "dca_orders": [
            {"price_percentage": float(f"{dca1_pct:.6f}"), "quantity_percentage": DCA1_QTY_PCT},
            {"price_percentage": float(f"{dca2_pct:.6f}"), "quantity_percentage": DCA2_QTY_PCT},
            {"price_percentage": float(f"{dca3_pct:.6f}"), "quantity_percentage": DCA3_QTY_PCT},
        ],

        "take_profit": [
            {"price_percentage": float(f"{tp1_pct:.6f}"), "position_percentage": TP1_PCT},
            {"price_percentage": float(f"{tp2_pct:.6f}"), "position_percentage": TP2_PCT},
            {"price_percentage": float(f"{tp3_pct:.6f}"), "position_percentage": TP3_PCT}
        ],

        "stop_loss": {
            **({"stop_percentage": float(f"{stop_percentage:.6f}")} if stop_percentage is not None else {}),
            "protection_type": "PRICE",
            "trailing_percentage": TRAILING_PERCENTAGE,
            "trailing_distance":  TRAILING_DISTANCE
        },

        "entry_expiration": { "time": ENTRY_EXPIRATION_MIN }
    }

    if abs((TP1_PCT + TP2_PCT + TP3_PCT + RUNNER_PCT) - 100.0) > 1e-6:
        print("⚠️ Hinweis: TP1+TP2+TP3+RUNNER != 100%. Prüfe ENV-Splits.")
    return payload

# =========================
# Sender
# =========================
def post_to_altrady(payload: dict):
    for attempt in range(3):
        try:
            r = requests.post(ALTRADY_WEBHOOK_URL, json=payload, timeout=20)
            if r.status_code == 429:
                delay = 2.0
                try:
                    delay = float(r.json().get("retry_after", 2.0))
                except:
                    pass
                time.sleep(delay + 0.25)
                continue
            r.raise_for_status()
            return r
        except Exception:
            if attempt == 2: raise
            time.sleep(1.5 * (attempt + 1))

# =========================
# Main
# =========================
def main():
    print(f"➡️ Altrady:{ALTRADY_EXCHANGE} | Quote:{QUOTE} | Lev:{FIXED_LEVERAGE}x | TP% {TP1_PCT}/{TP2_PCT}/{TP3_PCT} + Runner {RUNNER_PCT}%")
    print(f"   Trailing SL: {TRAILING_PERCENTAGE}% (dist {TRAILING_DISTANCE}%) | Hard SL {'ON' if USE_HARD_SL else 'OFF'} (±{SL_BUFFER_PCT}% v. DCA3)")
    print(f"   Entry Expiration: {ENTRY_EXPIRATION_MIN} min | Poll: {POLL_BASE_SECONDS}s + {POLL_OFFSET_SECONDS}s (+Jitter ≤ {POLL_JITTER_MAX}s)")
    state = load_state()
    last_id = state.get("last_id")

    while True:
        try:
            msg = fetch_latest_message(CHANNEL_ID)
            if not msg:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Kanal leer.")
            else:
                mid = msg.get("id")
                if last_id is None or int(mid) > int(last_id):
                    sig = parse_signal_from_message(msg)
                    if not sig:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Kein gültiges Signal erkannt.")
                    else:
                        print(f"[PARSED] {sig}")
                        payload = build_altrady_open_payload(sig)
                        post_to_altrady(payload)
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ an Altrady gesendet: {sig['base']} {sig['side']} @ {sig['entry']}")
                    last_id = mid
                    state["last_id"] = last_id
                    save_state(state)
                else:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Keine neuere Nachricht.")
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except requests.HTTPError as http_err:
            body = ""
            try:
                body = http_err.response.text[:200]
            except:
                pass
            print("[HTTP ERROR]", http_err.response.status_code, body or "")
        except Exception:
            print("[ERROR]")
            traceback.print_exc()
        finally:
            sleep_until_next_tick()

if __name__ == "__main__":
    main()
