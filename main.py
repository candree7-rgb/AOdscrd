#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Discord → Altrady Forwarder (New-Format + Thread Follow)
- Liest die jüngsten Nachrichten im Parent-Channel (z.B. 15 Stück)
- Wenn ein Post nur den Thread eröffnet (kein Signaltext), folgt er dem Thread
  und versucht dort die letzten Beiträge (z.B. 5) zu parsen
- Erkennt Format:

    <TICKER> (LONG/SHORT) Signal
    ...
    Enter on Trigger: $X.XXXX
    TP1: $...
    TP2: $...
    TP3: $...
    DCA #1: $...
    DCA #2: $...
    DCA #3: $...

- Rechnet absolute Preise in %-Abstände vom Entry um (für Altrady price_percentage)
- Baut Altrady "open" Payload mit:
    order_type=limit, signal_price=Entry
    leverage=25 (env)
    dca_orders: 150% / 225% / 340% (env)
    take_profit: 30/30/30 (env), Runner 10% via trailing stop (stop_loss)
    optionaler harter SL in % jenseits DCA3 (env)
- Schickt Payload an ALTRADY_WEBHOOK_URL
"""

import os, re, sys, time, json, traceback
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# =========================
# ENV
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID    = os.getenv("CHANNEL_ID", "").strip()

ALTRADY_WEBHOOK_URL = os.getenv("ALTRADY_WEBHOOK_URL", "").strip()
ALTRADY_API_KEY     = os.getenv("ALTRADY_API_KEY", "").strip()
ALTRADY_API_SECRET  = os.getenv("ALTRADY_API_SECRET", "").strip()
ALTRADY_EXCHANGE    = os.getenv("ALTRADY_EXCHANGE", "BYBI").strip()
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

ENTRY_EXPIRATION_MIN = int(os.getenv("ENTRY_EXPIRATION_MIN", "60"))

# Polling + history sizes
POLL_BASE_SECONDS    = int(os.getenv("POLL_BASE_SECONDS", "60"))
POLL_OFFSET_SECONDS  = int(os.getenv("POLL_OFFSET_SECONDS", "3"))
PARENT_FETCH_LIMIT   = int(os.getenv("PARENT_FETCH_LIMIT", "15"))
THREAD_FETCH_LIMIT   = int(os.getenv("THREAD_FETCH_LIMIT", "5"))

STATE_FILE           = Path(os.getenv("STATE_FILE", "state.json"))

# =========================
# Sanity
# =========================
if not DISCORD_TOKEN or not CHANNEL_ID or not ALTRADY_WEBHOOK_URL:
    print("Bitte ENV setzen: DISCORD_TOKEN, CHANNEL_ID, ALTRADY_WEBHOOK_URL (+ Keys).")
    sys.exit(1)

HEADERS = {
    "Authorization": DISCORD_TOKEN,     # User session token or bot token
    "User-Agent": "DiscordToAltrady-DCA/1.1"
}

# =========================
# Helpers
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

def sleep_until_next_tick():
    now = time.time()
    period_start = (now // POLL_BASE_SECONDS) * POLL_BASE_SECONDS
    next_tick = period_start + POLL_BASE_SECONDS + POLL_OFFSET_SECONDS
    if now < period_start + POLL_OFFSET_SECONDS:
        next_tick = period_start + POLL_OFFSET_SECONDS
    time.sleep(max(0, next_tick - now))

# =========================
# Discord fetch
# =========================

def fetch_messages(channel_id: str, limit: int = 15):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    r = requests.get(url, headers=HEADERS, params={"limit": limit}, timeout=15)
    if r.status_code == 429:
        retry = 5
        try: retry = float(r.json().get("retry_after", 5))
        except Exception: pass
        time.sleep(retry + 0.5)
        r = requests.get(url, headers=HEADERS, params={"limit": limit}, timeout=15)
    r.raise_for_status()
    return r.json()  # newest first

def fetch_thread_messages(thread_channel_id: str, limit: int = 5):
    # Threads sind eigene Channel-IDs – gleiche endpoint
    return fetch_messages(thread_channel_id, limit=limit)

# =========================
# Parsing (neues Format)
# =========================

PAIR_LINE   = re.compile(r"\b([A-Z0-9]{2,})\s+(LONG|SHORT)\s+Signal\b", re.I)
ENTER_LINE  = re.compile(r"Enter\s+on\s+Trigger:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
TP1_LINE    = re.compile(r"TP1:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
TP2_LINE    = re.compile(r"TP2:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
TP3_LINE    = re.compile(r"TP3:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
DCA1_LINE   = re.compile(r"DCA\s*#?1:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
DCA2_LINE   = re.compile(r"DCA\s*#?2:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)
DCA3_LINE   = re.compile(r"DCA\s*#?3:\s*\$?\s*([0-9]*\.?[0-9]+)", re.I)

THREAD_URL  = re.compile(r"https?://discord\.com/channels/\d+/(\d+)/\d+", re.I)

def _collect_text_from_message(msg: dict) -> str:
    parts = []
    parts.append((msg.get("content") or ""))
    embeds = msg.get("embeds") or []
    if embeds and isinstance(embeds, list):
        e0 = embeds[0] or {}
        desc = e0.get("description") or ""
        if desc: parts.append(desc)
        # Einige Bots packen Felder in embed.fields
        fields = e0.get("fields") or []
        for f in fields:
            name = (f.get("name") or "")
            val  = (f.get("value") or "")
            if name: parts.append(str(name))
            if val:  parts.append(str(val))
    return "\n".join([p for p in parts if p]).strip()

def parse_new_signal_block(text: str):
    t = (text or "").replace("\r","").strip()
    m_pair = PAIR_LINE.search(t)
    if not m_pair:
        return None
    base  = m_pair.group(1).upper()
    side  = "long" if m_pair.group(2).upper()=="LONG" else "short"

    m_e   = ENTER_LINE.search(t)
    m_tp1 = TP1_LINE.search(t)
    m_tp2 = TP2_LINE.search(t)
    m_tp3 = TP3_LINE.search(t)
    m_d1  = DCA1_LINE.search(t)
    m_d2  = DCA2_LINE.search(t)
    m_d3  = DCA3_LINE.search(t)

    if not (m_e and m_tp1 and m_tp2 and m_tp3 and m_d1 and m_d2 and m_d3):
        return None

    entry = float(m_e.group(1))
    tp1   = float(m_tp1.group(1))
    tp2   = float(m_tp2.group(1))
    tp3   = float(m_tp3.group(1))
    d1    = float(m_d1.group(1))
    d2    = float(m_d2.group(1))
    d3    = float(m_d3.group(1))

    if side == "long":
        ok = (tp1>entry and tp2>entry and tp3>entry and d1<entry and d2<entry and d3<entry)
    else:
        ok = (tp1<entry and tp2<entry and tp3<entry and d1>entry and d2>entry and d3>entry)
    if not ok:
        return None

    return {
        "base": base,
        "side": side,
        "entry": entry,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "dca1": d1, "dca2": d2, "dca3": d3
    }

def try_parse_message(msg: dict):
    text = _collect_text_from_message(msg)
    if not text:
        return None
    # Split grob in Abschnitte und suche ersten Treffer
    blocks = re.split(r"\n\s*\n", text)
    for b in blocks:
        parsed = parse_new_signal_block(b)
        if parsed:
            return parsed
    return None

def find_thread_id(msg: dict) -> str | None:
    # 1) native thread object?
    thr = msg.get("thread")
    if isinstance(thr, dict) and thr.get("id"):
        return str(thr["id"])
    # 2) link im text/embed
    text = _collect_text_from_message(msg)
    m = THREAD_URL.search(text)
    if m:
        return m.group(1)
    return None

# =========================
# Altrady Payload Builder
# =========================

def pct_dist(entry: float, price: float) -> float:
    return abs((price - entry) / entry) * 100.0

def build_altrady_open_payload(sig: dict) -> dict:
    base   = sig["base"]
    side   = sig["side"]
    entry  = sig["entry"]
    tp1    = sig["tp1"]; tp2 = sig["tp2"]; tp3 = sig["tp3"]
    d1     = sig["dca1"]; d2 = sig["dca2"]; d3 = sig["dca3"]

    symbol = f"{ALTRADY_EXCHANGE}_{QUOTE}_{base}"

    tp1_pct = pct_dist(entry, tp1)
    tp2_pct = pct_dist(entry, tp2)
    tp3_pct = pct_dist(entry, tp3)

    dca1_pct = pct_dist(entry, d1)
    dca2_pct = pct_dist(entry, d2)
    dca3_pct = pct_dist(entry, d3)

    stop_percentage = None
    if USE_HARD_SL:
        if side == "short":
            sl_price = d3 * (1.0 + SL_BUFFER_PCT/100.0)
        else:
            sl_price = d3 * (1.0 - SL_BUFFER_PCT/100.0)
        stop_percentage = pct_dist(entry, sl_price)

    payload = {
        "action": "open",
        "api_key": ALTRADY_API_KEY,
        "api_secret": ALTRADY_API_SECRET,
        "exchange": ALTRADY_EXCHANGE,
        "symbol": symbol,
        "side": side,                        # "long" | "short"
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
        print("⚠️ Hinweis: TP1+TP2+TP3+RUNNER != 100%. Prüfe deine ENV-Splits.")
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
                try: delay = float(r.json().get("retry_after", 2.0))
                except Exception: pass
                time.sleep(delay + 0.25)
                continue
            r.raise_for_status()
            return r
        except Exception:
            if attempt == 2: raise
            time.sleep(1.5 * (attempt + 1))

# =========================
# MAIN LOOP
# =========================

def main():
    print(f"➡️ Altrady:{ALTRADY_EXCHANGE} | Quote:{QUOTE} | Lev:{FIXED_LEVERAGE}x | TP% split {TP1_PCT}/{TP2_PCT}/{TP3_PCT}+{RUNNER_PCT}(runner)")
    print(f"   Trailing SL: {TRAILING_PERCENTAGE}% / dist {TRAILING_DISTANCE}% | Hard SL {'ON' if USE_HARD_SL else 'OFF'} (+{SL_BUFFER_PCT}% von DCA3)")
    state = load_state()
    last_id = state.get("last_id")

    while True:
        try:
            msgs = fetch_messages(CHANNEL_ID, limit=PARENT_FETCH_LIMIT)  # newest first
            # Filter: nur wirklich neue Nachrichten > last_id
            new_msgs = []
            if last_id is None:
                new_msgs = msgs[:]
            else:
                for m in msgs:
                    try:
                        if int(m["id"]) > int(last_id):
                            new_msgs.append(m)
                    except:
                        new_msgs.append(m)

            # in chronologischer Reihenfolge abarbeiten (alt -> neu)
            for msg in reversed(new_msgs):
                mid = msg.get("id")
                raw_preview = (_collect_text_from_message(msg) or "")[:180].replace("\n"," ⏎ ")
                parsed = try_parse_message(msg)
                if not parsed:
                    # Versuch: Thread folgen
                    thr_id = find_thread_id(msg)
                    if thr_id:
                        try:
                            tmsgs = fetch_thread_messages(thr_id, limit=THREAD_FETCH_LIMIT)
                            # auch hier alt->neu
                            for tmsg in reversed(tmsgs):
                                parsed = try_parse_message(tmsg)
                                if parsed:
                                    break
                        except Exception as e:
                            print(f"⚠️ Thread-Fetch Fehler {thr_id}: {e}")

                if not parsed:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Kein gültiges Signal erkannt.")
                    print(f"[RAW PREVIEW] {raw_preview}")
                else:
                    print(f"[PARSED] {parsed}")
                    payload = build_altrady_open_payload(parsed)
                    _ = post_to_altrady(payload)
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ an Altrady gesendet: {parsed['base']} {parsed['side']} @ {parsed['entry']}")
                # update last_id nach jeder abgearbeiteten neuen Nachricht
                last_id = mid
                state["last_id"] = last_id
                save_state(state)

            if not new_msgs:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Keine neuere Nachricht.")

        except KeyboardInterrupt:
            print("\nStopped."); break
        except requests.HTTPError as http_err:
            body = ""
            try: body = http_err.response.text[:200]
            except Exception: pass
            print("[HTTP ERROR]", http_err.response.status_code, body or "")
        except Exception:
            print("[ERROR]")
            traceback.print_exc()

        sleep_until_next_tick()

if __name__ == "__main__":
    main()
