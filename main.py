#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sys, time, json, traceback, html, random
from datetime import datetime
from pathlib import Path
from typing import Optional
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
ALTRADY_EXCHANGE    = os.getenv("ALTRADY_EXCHANGE", "BYBI").strip()   # z.B. BYBI, BYBIF, MEXC ...
QUOTE               = os.getenv("QUOTE", "USDT").strip().upper()

# Hebel
FIXED_LEVERAGE      = int(os.getenv("FIXED_LEVERAGE", "25"))

# TP-Split (30/30/30) + Runner (10% via SL/Trail abgesichert)
TP1_PCT             = float(os.getenv("TP1_PCT", "30"))
TP2_PCT             = float(os.getenv("TP2_PCT", "30"))
TP3_PCT             = float(os.getenv("TP3_PCT", "30"))
RUNNER_PCT          = float(os.getenv("RUNNER_PCT", "10"))

# Trailing-Stop (für Runner / globalen Stop-Block)
TRAILING_PERCENTAGE = float(os.getenv("TRAILING_PERCENTAGE", "3.0"))
TRAILING_DISTANCE   = float(os.getenv("TRAILING_DISTANCE", "0.5"))

# Initialer SL-Referenzpunkt & Schutz-Logik
STOP_PROTECTION_TYPE = os.getenv("STOP_PROTECTION_TYPE", "BREAK_EVEN").strip().upper()  # PRICE | BREAK_EVEN | FOLLOW_TAKE_PROFIT
BASE_STOP_MODE       = os.getenv("BASE_STOP_MODE", "DCA3").strip().upper()              # NONE | ENTRY | DCA3
SL_BUFFER_PCT        = float(os.getenv("SL_BUFFER_PCT", "5.0"))

# DCA Größen (% der Start-Positionsgröße)
DCA1_QTY_PCT        = float(os.getenv("DCA1_QTY_PCT", "150"))
DCA2_QTY_PCT        = float(os.getenv("DCA2_QTY_PCT", "225"))
DCA3_QTY_PCT        = float(os.getenv("DCA3_QTY_PCT", "340"))

# Falls DCA-Level im Signal fehlen: Distanz in % vom Entry
DCA1_DIST_PCT       = float(os.getenv("DCA1_DIST_PCT", "5"))
DCA2_DIST_PCT       = float(os.getenv("DCA2_DIST_PCT", "10"))
DCA3_DIST_PCT       = float(os.getenv("DCA3_DIST_PCT", "20"))

# Limit-Order Ablauf (Minuten)
ENTRY_EXPIRATION_MIN= int(os.getenv("ENTRY_EXPIRATION_MIN", "180"))

# Poll-Steuerung + Jitter
POLL_BASE_SECONDS   = int(os.getenv("POLL_BASE_SECONDS", "60"))
POLL_OFFSET_SECONDS = int(os.getenv("POLL_OFFSET_SECONDS", "3"))
POLL_JITTER_MAX     = int(os.getenv("POLL_JITTER_MAX", "7"))

# Fetch-Größe pro Page (Discord API max 100)
DISCORD_FETCH_LIMIT = int(os.getenv("DISCORD_FETCH_LIMIT", "50"))

STATE_FILE          = Path(os.getenv("STATE_FILE", "state.json"))

# =========================
# Sanity
# =========================
if not DISCORD_TOKEN or not CHANNEL_ID or not ALTRADY_WEBHOOK_URL:
    print("Bitte ENV setzen: DISCORD_TOKEN, CHANNEL_ID, ALTRADY_WEBHOOK_URL (+ Keys).")
    sys.exit(1)

HEADERS = {
    "Authorization": DISCORD_TOKEN,   # User-Session oder Bot-Token
    "User-Agent": "DiscordToAltrady-DCA/1.7"
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

def fetch_messages_after(channel_id: str, after_id: Optional[str], limit: int = 50):
    """
    Holt Messages > after_id (strictly newer). Discord unterstützt 'after'.
    Wir paginieren, bis weniger als 'limit' zurückkommt.
    """
    collected = []
    params = {"limit": max(1, min(limit, 100))}
    if after_id:
        params["after"] = str(after_id)

    while True:
        r = requests.get(f"https://discord.com/api/v10/channels/{channel_id}/messages",
                         headers=HEADERS, params=params, timeout=15)
        if r.status_code == 429:
            retry = 5
            try:
                if r.headers.get("Content-Type","").startswith("application/json"):
                    retry = float(r.json().get("retry_after", 5))
            except:
                pass
            time.sleep(retry + 0.5)
            continue
        r.raise_for_status()
        page = r.json() or []
        collected.extend(page)
        if len(page) < params["limit"]:
            break
        # weiter paginieren ab dem neuesten (höchste ID) der Page
        max_id = max(int(m.get("id","0")) for m in page if "id" in m)
        params["after"] = str(max_id)
    return collected

# =========================
# Cleaning (Markdown -> Plain)
# =========================
MD_LINK   = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
MD_MARK   = re.compile(r"[*_`~]+")
MULTI_WS  = re.compile(r"[ \t\u00A0]+")
NUM       = r"([0-9][0-9,]*\.?[0-9]*)"  # erlaubt auch 105,000.00

def clean_markdown(s: str) -> str:
    if not s: return ""
    s = s.replace("\r", "")
    s = html.unescape(s)
    s = MD_LINK.sub(r"\1", s)     # [text](url) -> text
    s = MD_MARK.sub("", s)        # *, _, `, ~ entfernen
    s = MULTI_WS.sub(" ", s)      # mehrfachen Whitespace normalisieren
    s = "\n".join(line.strip() for line in s.split("\n"))
    return s.strip()

def to_price(s: str) -> float:
    return float(s.replace(",", ""))

def message_text(m: dict) -> str:
    """
    Kombiniert content + Embeds (title, description, fields, footer),
    damit auch App/Live-Formate vollständig sind.
    """
    parts = []
    parts.append(m.get("content") or "")
    embeds = m.get("embeds") or []
    for e in embeds:
        if not isinstance(e, dict):
            continue
        if e.get("title"): parts.append(str(e.get("title")))
        if e.get("description"): parts.append(str(e.get("description")))
        fields = e.get("fields") or []
        for f in fields:
            if not isinstance(f, dict):
                continue
            n = f.get("name") or ""
            v = f.get("value") or ""
            if n: parts.append(str(n))
            if v: parts.append(str(v))
        footer = (e.get("footer") or {}).get("text")
        if footer: parts.append(str(footer))
    return clean_markdown("\n".join([p for p in parts if p]))

# =========================
# Parsing (mehrere Formate, tolerant)
# =========================

# 1) Klassisch:  TICKER LONG|SHORT Signal
PAIR_LINE_OLD   = re.compile(r"(^|\n)\s*([A-Z0-9]+)\s+(LONG|SHORT)\s+Signal\s*(\n|$)", re.I)

# 2) Header mit Slash:  🔴 PIPPIN/USDT SHORT • Leverage ...
HDR_SLASH_PAIR  = re.compile(r"([A-Z0-9]+)\s*/\s*[A-Z0-9]+\b.*\b(LONG|SHORT)\b", re.I)

# 3) „Coin: SD … Direction: SHORT“
HDR_COIN_DIR    = re.compile(r"Coin\s*:\s*([A-Z0-9]+).*?Direction\s*:\s*(LONG|SHORT)", re.I | re.S)

# Entry-Varianten
ENTER_ON_TRIGGER = re.compile(r"Enter\s+on\s+Trigger\s*:\s*\$?\s*"+NUM, re.I)
ENTRY_COLON      = re.compile(r"\bEntry\s*:\s*\$?\s*"+NUM, re.I)
ENTRY_SECTION    = re.compile(r"\bENTRY\b\s*\n\s*\$?\s*"+NUM, re.I)

# Generische Ziele / DCA
TP1_LINE        = re.compile(r"\bTP\s*1\s*:\s*\$?\s*"+NUM, re.I)
TP2_LINE        = re.compile(r"\bTP\s*2\s*:\s*\$?\s*"+NUM, re.I)
TP3_LINE        = re.compile(r"\bTP\s*3\s*:\s*\$?\s*"+NUM, re.I)
DCA1_LINE       = re.compile(r"\bDCA\s*#?\s*1\s*:\s*\$?\s*"+NUM, re.I)
DCA2_LINE       = re.compile(r"\bDCA\s*#?\s*2\s*:\s*\$?\s*"+NUM, re.I)
DCA3_LINE       = re.compile(r"\bDCA\s*#?\s*3\s*:\s*\$?\s*"+NUM, re.I)

def find_base_side(txt: str):
    # Prio: Slash-Header -> Classic „SHORT Signal“ -> Coin/Direction
    mh = HDR_SLASH_PAIR.search(txt)
    if mh:
        return mh.group(1).upper(), ("long" if mh.group(2).upper()=="LONG" else "short")
    mo = PAIR_LINE_OLD.search(txt)
    if mo:
        return mo.group(2).upper(), ("long" if mo.group(3).upper()=="LONG" else "short")
    mc = HDR_COIN_DIR.search(txt)
    if mc:
        return mc.group(1).upper(), ("long" if mc.group(2).upper()=="LONG" else "short")
    return None, None

def find_entry(txt: str) -> Optional[float]:
    for rx in (ENTER_ON_TRIGGER, ENTRY_COLON, ENTRY_SECTION):
        m = rx.search(txt)
        if m:
            return to_price(m.group(1))
    return None

def find_tp_dca(txt: str):
    tps = []
    for rx in (TP1_LINE, TP2_LINE, TP3_LINE):
        m = rx.search(txt)
        tps.append(to_price(m.group(1)) if m else None)
    dcas = []
    for rx in (DCA1_LINE, DCA2_LINE, DCA3_LINE):
        m = rx.search(txt)
        dcas.append(to_price(m.group(1)) if m else None)
    return tps, dcas

def backfill_dcas_if_missing(side: str, entry: float, dcas: list) -> list:
    """
    Ergänzt fehlende DCA-Preise anhand ENV-% vom Entry.
    SHORT: DCA über Entry, LONG: DCA unter Entry.
    """
    d1, d2, d3 = dcas
    if d1 is None:
        d1 = entry * (1 + DCA1_DIST_PCT/100.0) if side=="short" else entry * (1 - DCA1_DIST_PCT/100.0)
    if d2 is None:
        d2 = entry * (1 + DCA2_DIST_PCT/100.0) if side=="short" else entry * (1 - DCA2_DIST_PCT/100.0)
    if d3 is None:
        d3 = entry * (1 + DCA3_DIST_PCT/100.0) if side=="short" else entry * (1 - DCA3_DIST_PCT/100.0)
    return [d1, d2, d3]

def plausible(side: str, entry: float, tp1: float, tp2: float, tp3: float, d1: float, d2: float, d3: float) -> bool:
    if side == "long":
        return (tp1>entry and tp2>entry and tp3>entry and d1<entry and d2<entry and d3<entry)
    else:
        return (tp1<entry and tp2<entry and tp3<entry and d1>entry and d2>entry and d3>entry)

def parse_signal_from_text(txt: str):
    base, side = find_base_side(txt)
    if not base or not side:
        return None
    entry = find_entry(txt)
    if entry is None:
        return None

    (tp1, tp2, tp3), (d1, d2, d3) = find_tp_dca(txt)

    # TPs müssen vorhanden sein
    if None in (tp1, tp2, tp3):
        return None

    # DCA optional -> ggf. auffüllen
    d1, d2, d3 = backfill_dcas_if_missing(side, entry, [d1, d2, d3])

    if not plausible(side, entry, tp1, tp2, tp3, d1, d2, d3):
        return None

    return {
        "base": base, "side": side, "entry": entry,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "dca1": d1,  "dca2": d2,  "dca3": d3
    }

# =========================
# Payload Builder
# =========================
def pct_dist(entry: float, price: float) -> float:
    return abs((price - entry) / entry) * 100.0

def compute_base_stop_percentage(side: str, entry: float, d3: float) -> Optional[float]:
    """
    Liefert die stop_percentage relativ zum Entry (in %), basierend auf BASE_STOP_MODE.
    - ENTRY: SL = Entry ± SL_BUFFER_PCT
    - DCA3 : SL = DCA3 ± SL_BUFFER_PCT
    - NONE : kein initialer SL
    """
    mode = BASE_STOP_MODE
    if mode == "NONE":
        return None
    if mode == "ENTRY":
        sl_price = entry * (1 - SL_BUFFER_PCT/100.0) if side == "long" else entry * (1 + SL_BUFFER_PCT/100.0)
    else:  # "DCA3" default
        sl_price = d3 * (1 - SL_BUFFER_PCT/100.0) if side == "long" else d3 * (1 + SL_BUFFER_PCT/100.0)
    return pct_dist(entry, sl_price)

def build_altrady_open_payload(sig: dict) -> dict:
    base, side, entry = sig["base"], sig["side"], sig["entry"]
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
    d1, d2, d3 = sig["dca1"], sig["dca2"], sig["dca3"]

    symbol = f"{ALTRADY_EXCHANGE}_{QUOTE}_{base}"

    tp1_pct  = pct_dist(entry, tp1)
    tp2_pct  = pct_dist(entry, tp2)
    tp3_pct  = pct_dist(entry, tp3)
    dca1_pct = pct_dist(entry, d1)
    dca2_pct = pct_dist(entry, d2)
    dca3_pct = pct_dist(entry, d3)

    # Initialen SL IMMER mitsenden (sichtbar in Altrady), Schutz-Logik via STOP_PROTECTION_TYPE
    base_stop_pct = compute_base_stop_percentage(side, entry, d3)

    stop_loss_obj = {
        "protection_type": STOP_PROTECTION_TYPE,         # PRICE | BREAK_EVEN | FOLLOW_TAKE_PROFIT
        "trailing_percentage": TRAILING_PERCENTAGE,
        "trailing_distance":  TRAILING_DISTANCE
    }
    if base_stop_pct is not None:
        stop_loss_obj["stop_percentage"] = float(f"{base_stop_pct:.6f}")

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

        "stop_loss": stop_loss_obj,
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
                try: delay = float(r.json().get("retry_after", 2.0))
                except: pass
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
    print(f"   Stop: {STOP_PROTECTION_TYPE} | Base:{BASE_STOP_MODE} (±{SL_BUFFER_PCT}%) | Trail {TRAILING_PERCENTAGE}% / dist {TRAILING_DISTANCE}%")
    print(f"   Entry Expiration: {ENTRY_EXPIRATION_MIN} min | Poll: {POLL_BASE_SECONDS}s + {POLL_OFFSET_SECONDS}s (+Jitter ≤ {POLL_JITTER_MAX}s) | Fetch page ≤ {DISCORD_FETCH_LIMIT}")

    state = load_state()
    last_id = state.get("last_id")

    # Erststart: baseline auf aktuellste Message setzen (nicht rückwirkend spammen)
    if last_id is None:
        try:
            page = fetch_messages_after(CHANNEL_ID, None, limit=1)
            if page:
                last_id = str(page[0]["id"])
                state["last_id"] = last_id
                save_state(state)
        except Exception:
            pass

    while True:
        try:
            msgs = fetch_messages_after(CHANNEL_ID, last_id, limit=DISCORD_FETCH_LIMIT)
            # Discord liefert „neueste zuerst“, sortieren aufsteigend, damit wir chronologisch verarbeiten
            msgs_sorted = sorted(msgs, key=lambda m: int(m.get("id","0")))
            max_seen = int(last_id or 0)

            if not msgs_sorted:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Keine neuen Nachrichten.")
            else:
                for m in msgs_sorted:
                    mid = int(m.get("id","0"))
                    raw = message_text(m)
                    if raw:
                        print("[RAW PREVIEW CLEANED]")
                        print("\n".join(raw.split("\n")[:80]))
                        sig = parse_signal_from_text(raw)
                        if sig:
                            print(f"[PARSED] {sig}")
                            payload = build_altrady_open_payload(sig)
                            post_to_altrady(payload)
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ an Altrady gesendet: {sig['base']} {sig['side']} @ {sig['entry']}")
                        else:
                            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Kein gültiges Signal in dieser Message.")
                    max_seen = max(max_seen, mid)

                last_id = str(max_seen)
                state["last_id"] = last_id
                save_state(state)

        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except requests.HTTPError as http_err:
            body = ""
            try: body = http_err.response.text[:200]
            except: pass
            print("[HTTP ERROR]", http_err.response.status_code, body or "")
        except Exception:
            print("[ERROR]")
            traceback.print_exc()
        finally:
            sleep_until_next_tick()

if __name__ == "__main__":
    main()
