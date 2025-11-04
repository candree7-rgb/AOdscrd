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
ALTRADY_EXCHANGE    = os.getenv("ALTRADY_EXCHANGE", "BYBI").strip()
QUOTE               = os.getenv("QUOTE", "USDT").strip().upper()

# Hebel
FIXED_LEVERAGE      = int(os.getenv("FIXED_LEVERAGE", "25"))

# TP-Split (30/30/30) + Runner (10% via SL/Trail abgesichert)
TP1_PCT             = float(os.getenv("TP1_PCT", "30"))
TP2_PCT             = float(os.getenv("TP2_PCT", "30"))
TP3_PCT             = float(os.getenv("TP3_PCT", "30"))
RUNNER_PCT          = float(os.getenv("RUNNER_PCT", "10"))

# Trailing für Runner (nur beim letzten TP!)
RUNNER_TRAILING_DIST = float(os.getenv("RUNNER_TRAILING_DIST", "1.5"))  
RUNNER_TP_MULTIPLIER = float(os.getenv("RUNNER_TP_MULTIPLIER", "1.5"))  

# Stop-Loss Protection Einstellungen
STOP_PROTECTION_TYPE = os.getenv("STOP_PROTECTION_TYPE", "FOLLOW_TAKE_PROFIT").strip().upper()
BASE_STOP_MODE       = os.getenv("BASE_STOP_MODE", "DCA3").strip().upper()
SL_BUFFER_PCT        = float(os.getenv("SL_BUFFER_PCT", "5.0"))

# DCA Größen (% der Start-Positionsgröße)
DCA1_QTY_PCT        = float(os.getenv("DCA1_QTY_PCT", "150"))
DCA2_QTY_PCT        = float(os.getenv("DCA2_QTY_PCT", "225"))
DCA3_QTY_PCT        = float(os.getenv("DCA3_QTY_PCT", "340"))

# Fallback DCA-Distanzen (falls im Signal keine DCAs angegeben)
DCA1_DIST_PCT       = float(os.getenv("DCA1_DIST_PCT", "5"))
DCA2_DIST_PCT       = float(os.getenv("DCA2_DIST_PCT", "10"))
DCA3_DIST_PCT       = float(os.getenv("DCA3_DIST_PCT", "20"))

# Limit-Order Ablauf (Minuten)
ENTRY_EXPIRATION_MIN= int(os.getenv("ENTRY_EXPIRATION_MIN", "180"))

# Poll-Steuerung
POLL_BASE_SECONDS   = int(os.getenv("POLL_BASE_SECONDS", "60"))
POLL_OFFSET_SECONDS = int(os.getenv("POLL_OFFSET_SECONDS", "3"))
POLL_JITTER_MAX     = int(os.getenv("POLL_JITTER_MAX", "7"))

# Fetch-Größe pro Page
DISCORD_FETCH_LIMIT = int(os.getenv("DISCORD_FETCH_LIMIT", "50"))

STATE_FILE          = Path(os.getenv("STATE_FILE", "state.json"))

# Test Mode (für Entwicklung)
TEST_MODE           = os.getenv("TEST_MODE", "false").lower() == "true"

# =========================
# Sanity Check
# =========================
if not DISCORD_TOKEN or not CHANNEL_ID or not ALTRADY_WEBHOOK_URL:
    print("❌ Bitte ENV setzen: DISCORD_TOKEN, CHANNEL_ID, ALTRADY_WEBHOOK_URL")
    sys.exit(1)

if not ALTRADY_API_KEY or not ALTRADY_API_SECRET:
    print("❌ Bitte API Keys setzen: ALTRADY_API_KEY, ALTRADY_API_SECRET")
    sys.exit(1)

HEADERS = {
    "Authorization": DISCORD_TOKEN,
    "User-Agent": "DiscordToAltrady-DCA/2.0"
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
    """Holt Messages > after_id (strictly newer)."""
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
        max_id = max(int(m.get("id","0")) for m in page if "id" in m)
        params["after"] = str(max_id)
    return collected

# =========================
# Text Cleaning
# =========================
MD_LINK   = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
MD_MARK   = re.compile(r"[*_`~]+")
MULTI_WS  = re.compile(r"[ \t\u00A0]+")
NUM       = r"([0-9][0-9,]*\.?[0-9]*)"

def clean_markdown(s: str) -> str:
    if not s: return ""
    s = s.replace("\r", "")
    s = html.unescape(s)
    s = MD_LINK.sub(r"\1", s)
    s = MD_MARK.sub("", s)
    s = MULTI_WS.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return s.strip()

def to_price(s: str) -> float:
    return float(s.replace(",", ""))

def message_text(m: dict) -> str:
    """Kombiniert content + Embeds für vollständigen Text."""
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
# Signal Parsing
# =========================

# Pattern Definitionen
PAIR_LINE_OLD   = re.compile(r"(^|\n)\s*([A-Z0-9]+)\s+(LONG|SHORT)\s+Signal\s*(\n|$)", re.I)
HDR_SLASH_PAIR  = re.compile(r"([A-Z0-9]+)\s*/\s*[A-Z0-9]+\b.*\b(LONG|SHORT)\b", re.I)
HDR_COIN_DIR    = re.compile(r"Coin\s*:\s*([A-Z0-9]+).*?Direction\s*:\s*(LONG|SHORT)", re.I | re.S)

ENTER_ON_TRIGGER = re.compile(r"Enter\s+on\s+Trigger\s*:\s*\$?\s*"+NUM, re.I)
ENTRY_COLON      = re.compile(r"\bEntry\s*:\s*\$?\s*"+NUM, re.I)
ENTRY_SECTION    = re.compile(r"\bENTRY\b\s*\n\s*\$?\s*"+NUM, re.I)

TP1_LINE        = re.compile(r"\bTP\s*1\s*:\s*\$?\s*"+NUM, re.I)
TP2_LINE        = re.compile(r"\bTP\s*2\s*:\s*\$?\s*"+NUM, re.I)
TP3_LINE        = re.compile(r"\bTP\s*3\s*:\s*\$?\s*"+NUM, re.I)
DCA1_LINE       = re.compile(r"\bDCA\s*#?\s*1\s*:\s*\$?\s*"+NUM, re.I)
DCA2_LINE       = re.compile(r"\bDCA\s*#?\s*2\s*:\s*\$?\s*"+NUM, re.I)
DCA3_LINE       = re.compile(r"\bDCA\s*#?\s*3\s*:\s*\$?\s*"+NUM, re.I)

def find_base_side(txt: str):
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
    """Ergänzt fehlende DCA-Preise mit Fallback-Werten."""
    d1, d2, d3 = dcas
    if d1 is None:
        d1 = entry * (1 + DCA1_DIST_PCT/100.0) if side=="short" else entry * (1 - DCA1_DIST_PCT/100.0)
    if d2 is None:
        d2 = entry * (1 + DCA2_DIST_PCT/100.0) if side=="short" else entry * (1 - DCA2_DIST_PCT/100.0)
    if d3 is None:
        d3 = entry * (1 + DCA3_DIST_PCT/100.0) if side=="short" else entry * (1 - DCA3_DIST_PCT/100.0)
    return [d1, d2, d3]

def plausible(side: str, entry: float, tp1: float, tp2: float, tp3: float, d1: float, d2: float, d3: float) -> bool:
    """Validiert ob die Preise für die Richtung Sinn machen."""
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

    # DCA optional -> ggf. auffüllen mit Fallback
    d1, d2, d3 = backfill_dcas_if_missing(side, entry, [d1, d2, d3])

    if not plausible(side, entry, tp1, tp2, tp3, d1, d2, d3):
        return None

    return {
        "base": base, "side": side, "entry": entry,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "dca1": d1, "dca2": d2, "dca3": d3
    }

# =========================
# Altrady Payload Builder
# =========================
def pct_dist(entry: float, price: float) -> float:
    """Berechnet prozentuale Distanz zwischen zwei Preisen."""
    return abs((price - entry) / entry) * 100.0

def compute_base_stop_percentage(side: str, entry: float, d3: float) -> float:
    """
    Berechnet den Stop-Loss mit dynamischem Buffer basierend auf DCA3-Distanz.
    Bei weit entfernten DCA3 (>30%) wird der Buffer erhöht.
    """
    mode = BASE_STOP_MODE
    dca3_dist = pct_dist(entry, d3)
    
    # Dynamischer Buffer: Bei DCA3 > 30% mehr Spielraum
    if dca3_dist > 30:
        buffer = 10.0  # Doppelter Buffer bei extremen DCAs
        print(f"   ⚠️ DCA3 ist {dca3_dist:.1f}% entfernt - erhöhe SL Buffer auf {buffer}%")
    else:
        buffer = SL_BUFFER_PCT
    
    if mode == "NONE":
        return 50.0  # Notfall-SL bei 50%
    elif mode == "ENTRY":
        sl_price = entry * (1 - buffer/100.0) if side == "long" else entry * (1 + buffer/100.0)
    else:  # "DCA3" default
        sl_price = d3 * (1 - buffer/100.0) if side == "long" else d3 * (1 + buffer/100.0)
    
    return pct_dist(entry, sl_price)

def build_altrady_open_payload(sig: dict) -> dict:
    """Baut das komplette Altrady Webhook Payload."""
    base, side, entry = sig["base"], sig["side"], sig["entry"]
    tp1, tp2, tp3 = sig["tp1"], sig["tp2"], sig["tp3"]
    d1, d2, d3 = sig["dca1"], sig["dca2"], sig["dca3"]

    symbol = f"{ALTRADY_EXCHANGE}_{QUOTE}_{base}"

    # Berechne alle Prozente
    tp1_pct  = pct_dist(entry, tp1)
    tp2_pct  = pct_dist(entry, tp2)
    tp3_pct  = pct_dist(entry, tp3)
    dca1_pct = pct_dist(entry, d1)
    dca2_pct = pct_dist(entry, d2)
    dca3_pct = pct_dist(entry, d3)

    # Stop-Loss mit dynamischem Buffer
    base_stop_pct = compute_base_stop_percentage(side, entry, d3)
    
    print(f"\n📊 Signal Details:")
    print(f"   Symbol: {symbol} | Side: {side.upper()}")
    print(f"   Entry: ${entry:.10f}")
    print(f"   TPs: {tp1_pct:.1f}% | {tp2_pct:.1f}% | {tp3_pct:.1f}%")
    print(f"   DCAs: {dca1_pct:.1f}% | {dca2_pct:.1f}% | {dca3_pct:.1f}%")
    print(f"   Stop-Loss: {base_stop_pct:.1f}% ({BASE_STOP_MODE})")
    
    # Stop-Loss mit Protection Type
    stop_loss_obj = {
        "stop_percentage": float(f"{base_stop_pct:.6f}"),
        "protection_type": STOP_PROTECTION_TYPE
    }

    # Take Profits (3 normale + 1 Runner mit Trailing)
    take_profits = [
        {"price_percentage": float(f"{tp1_pct:.6f}"), "position_percentage": TP1_PCT},
        {"price_percentage": float(f"{tp2_pct:.6f}"), "position_percentage": TP2_PCT},
        {"price_percentage": float(f"{tp3_pct:.6f}"), "position_percentage": TP3_PCT}
    ]
    
    # Runner TP (10% mit Trailing)
    if RUNNER_PCT > 0:
        runner_tp_pct = tp3_pct * RUNNER_TP_MULTIPLIER
        take_profits.append({
            "price_percentage": float(f"{runner_tp_pct:.6f}"),
            "position_percentage": RUNNER_PCT,
            "trailing_distance": RUNNER_TRAILING_DIST
        })
        print(f"   Runner: {RUNNER_PCT}% @ {runner_tp_pct:.1f}% mit {RUNNER_TRAILING_DIST}% Trail")

    # Hauptpayload
    payload = {
        "action": "open",
        "api_key": ALTRADY_API_KEY,
        "api_secret": ALTRADY_API_SECRET,
        "exchange": ALTRADY_EXCHANGE,
        "symbol": symbol,
        "side": side,
        "order_type": "limit",
        "signal_price": float(f"{entry:.10f}"),
        "leverage": FIXED_LEVERAGE,
        
        # DCA Orders - Altrady überwacht diese automatisch
        "dca_orders": [
            {"price_percentage": float(f"{dca1_pct:.6f}"), "quantity_percentage": DCA1_QTY_PCT},
            {"price_percentage": float(f"{dca2_pct:.6f}"), "quantity_percentage": DCA2_QTY_PCT},
            {"price_percentage": float(f"{dca3_pct:.6f}"), "quantity_percentage": DCA3_QTY_PCT},
        ],
        
        "take_profit": take_profits,
        "stop_loss": stop_loss_obj,
        "entry_expiration": {"time": ENTRY_EXPIRATION_MIN}
    }

    # Test Mode
    if TEST_MODE:
        payload["test"] = True
        print("   🧪 TEST MODE: Nur Pending Orders werden erstellt")

    # Validierung
    total_tp_pct = TP1_PCT + TP2_PCT + TP3_PCT + RUNNER_PCT
    if abs(total_tp_pct - 100.0) > 0.01:
        print(f"   ⚠️ Warnung: TP-Summe = {total_tp_pct}% (sollte 100% sein)")
    
    return payload

# =========================
# HTTP Sender
# =========================
def post_to_altrady(payload: dict):
    """Sendet Payload an Altrady mit Retry-Logic."""
    if TEST_MODE:
        print("\n🧪 [TEST MODE] Würde senden:")
        print(json.dumps(payload, indent=2))
        return
    
    print("\n📤 Sende an Altrady...")
    
    for attempt in range(3):
        try:
            r = requests.post(ALTRADY_WEBHOOK_URL, json=payload, timeout=20)
            if r.status_code == 429:
                delay = 2.0
                try:
                    delay = float(r.json().get("retry_after", 2.0))
                except:
                    pass
                print(f"   Rate limited, warte {delay}s...")
                time.sleep(delay + 0.25)
                continue
            
            if r.status_code == 204:
                print("   ✅ Erfolgreich gesendet!")
                return r
            
            r.raise_for_status()
            return r
            
        except requests.HTTPError as e:
            print(f"   ❌ HTTP Error {e.response.status_code}: {e.response.text[:200]}")
            if attempt == 2:
                raise
        except Exception as e:
            print(f"   ❌ Fehler: {str(e)}")
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))

# =========================
# Main Loop
# =========================
def main():
    print("="*60)
    print("🚀 Discord → Altrady Signal Bot v2.0")
    print("="*60)
    print(f"📊 Konfiguration:")
    print(f"   Exchange: {ALTRADY_EXCHANGE} | Quote: {QUOTE}")
    print(f"   Leverage: {FIXED_LEVERAGE}x | Entry Expiry: {ENTRY_EXPIRATION_MIN}min")
    print(f"   TPs: {TP1_PCT}% / {TP2_PCT}% / {TP3_PCT}% + Runner {RUNNER_PCT}%")
    print(f"   DCAs: {DCA1_QTY_PCT}% / {DCA2_QTY_PCT}% / {DCA3_QTY_PCT}%")
    print(f"   Stop: {STOP_PROTECTION_TYPE} | Mode: {BASE_STOP_MODE} | Buffer: {SL_BUFFER_PCT}%")
    print(f"   Poll: {POLL_BASE_SECONDS}s | Test Mode: {TEST_MODE}")
    print("="*60)

    state = load_state()
    last_id = state.get("last_id")

    # Beim ersten Start: Baseline setzen
    if last_id is None:
        try:
            print("🔍 Initialisiere mit letzter Message...")
            page = fetch_messages_after(CHANNEL_ID, None, limit=1)
            if page:
                last_id = str(page[0]["id"])
                state["last_id"] = last_id
                save_state(state)
                print(f"   Start ab Message ID: {last_id}")
        except Exception as e:
            print(f"   ⚠️ Init-Fehler: {e}")

    print("\n👀 Überwache Discord Channel...")
    print("-"*40)

    while True:
        try:
            msgs = fetch_messages_after(CHANNEL_ID, last_id, limit=DISCORD_FETCH_LIMIT)
            msgs_sorted = sorted(msgs, key=lambda m: int(m.get("id","0")))
            max_seen = int(last_id or 0)

            if not msgs_sorted:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 💤 Keine neuen Nachrichten")
            else:
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 📨 {len(msgs_sorted)} neue Nachrichten")
                
                for m in msgs_sorted:
                    mid = int(m.get("id","0"))
                    raw = message_text(m)
                    
                    if raw:
                        # Preview der Message
                        preview = raw[:150].replace("\n", " ")
                        print(f"\n🔍 Analysiere: {preview}...")
                        
                        sig = parse_signal_from_text(raw)
                        
                        if sig:
                            print(f"✅ Signal erkannt!")
                            payload = build_altrady_open_payload(sig)
                            post_to_altrady(payload)
                        else:
                            print(f"❌ Kein gültiges Signal gefunden")
                    
                    max_seen = max(max_seen, mid)

                # State speichern
                last_id = str(max_seen)
                state["last_id"] = last_id
                save_state(state)

        except KeyboardInterrupt:
            print("\n\n👋 Bot gestoppt.")
            break
        except requests.HTTPError as e:
            print(f"\n❌ HTTP ERROR: {e}")
            time.sleep(5)
        except Exception:
            print(f"\n❌ FEHLER:")
            traceback.print_exc()
            time.sleep(10)
        finally:
            sleep_until_next_tick()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Beendet.")
    except Exception as e:
        print(f"\n💥 Kritischer Fehler: {e}")
        traceback.print_exc()
        sys.exit(1)
