# main.py
import os, re, hmac, json, time, asyncio, hashlib, math
from datetime import datetime
from typing import Dict, Any, Optional, Tuple, List
import httpx
from fastapi import FastAPI, Request, HTTPException

# ========= ENV =========
BYBIT_BASE       = os.getenv("BYBIT_BASE", "https://api-testnet.bybit.com")
BYBIT_KEY        = os.getenv("BYBIT_KEY", "")
BYBIT_SECRET     = os.getenv("BYBIT_SECRET", "")
SETTLE_COIN      = os.getenv("SETTLE_COIN", "USDT")

# klassische Signale: "Timeframe: H1/M15/M5" etc. – DCA-Format hat meist kein TF
ALLOWED_TFS      = set(os.getenv("ALLOWED_TFS", "H1,M15,M5").replace(" ", "").split(","))

# TP-Splits (klassisch 2 TPs) bleiben für Legacy-Parser, aber wir nutzen unten 3er-Splits:
TP1_POS_PCT      = float(os.getenv("TP1_POS_PCT", "20"))
TP2_POS_PCT      = float(os.getenv("TP2_POS_PCT", "80"))

# NEU: 3-TP-Splits (Summe ≤100). Default 30/30/30
TP_SPLITS_THREE  = os.getenv("TP_SPLITS_THREE", "30,30,30")
TP_THREE         = [int(x) for x in TP_SPLITS_THREE.replace(" ","").split(",")]
if len(TP_THREE) != 3 or sum(TP_THREE) > 100:
    TP_THREE = [30,30,30]

# Hebel: entweder fest (FIX_LEVERAGE) oder wie gehabt dynamisch aus SL-Distanz
FIX_LEVERAGE     = int(os.getenv("FIX_LEVERAGE", "0"))  # 0 = aus SL-Distanz
MAX_LEV_CAP      = int(os.getenv("MAX_LEV_CAP", "75"))
SAFETY_PCT       = float(os.getenv("SAFETY_PCT", "80"))

COOLDOWN_MIN     = int(os.getenv("COOLDOWN_MIN", "45"))     # nach FILL
ENTRY_EXP_MIN    = int(os.getenv("ENTRY_EXP_MIN", "60"))    # Ablauf Entry/DCA-Limits
DD_LIMIT_PCT     = float(os.getenv("DD_LIMIT_PCT", "2.8"))

MAX_OPEN_LONGS   = int(os.getenv("MAX_OPEN_LONGS", "999"))
MAX_OPEN_SHORTS  = int(os.getenv("MAX_OPEN_SHORTS", "999"))

TEXT_PATH        = os.getenv("TEXT_PATH", "content")
DEFAULT_NOTIONAL = float(os.getenv("DEFAULT_NOTIONAL", "50"))  # Margin (Starttranche) ohne Hebel

# DCA-Gewichte: 4 Werte (Initial, DCA1, DCA2, DCA3)
DCA_SCALES       = [float(x) for x in os.getenv("DCA_SCALES", "1,1.5,2.25,3.4").replace(" ","").split(",")]
if len(DCA_SCALES) != 4 or DCA_SCALES[0] <= 0:
    DCA_SCALES = [1,1.5,2.25,3.4]

# Stop-Loss relativ zu DCA3 (in Prozent; Short: +%, Long: -%)
SL_OVER_DCA3_PCT = float(os.getenv("SL_OVER_DCA3_PCT", "38.0"))

# ========= App / State =========
app = FastAPI()
_httpx_client: Optional[httpx.AsyncClient] = None

STATE: Dict[str, Any] = {
    "last_trade_ts": 0.0,              # wird erst bei (Teil-)Fill gesetzt
    "open_watch": {}                   # symbol -> meta
}

SYMBOL_META: Dict[str, Dict[str, float]] = {}  # tickSize/stepSize/minQty Cache

def now_ts() -> float: return time.time()

# ========= FastAPI Lifecycle =========
@app.on_event("startup")
async def _startup():
    global _httpx_client
    _httpx_client = httpx.AsyncClient(timeout=15.0)
    asyncio.create_task(monitor_loop())
    print("✅ Server started, monitor loop running...")

@app.on_event("shutdown")
async def _shutdown():
    global _httpx_client
    if _httpx_client:
        await _httpx_client.aclose()
        _httpx_client = None

# ========= Bybit Client (SignType=2) =========
def _qs(params: Dict[str, Any]) -> str:
    items = [(k, str(v)) for k, v in params.items() if v not in (None, "")]
    items.sort(key=lambda kv: kv[0])
    return "&".join(f"{k}={v}" for k, v in items)

async def bybit(path: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if not BYBIT_KEY or not BYBIT_SECRET:
        raise HTTPException(500, "BYBIT_KEY/SECRET not set")
    ts = str(int(now_ts()*1000))
    recv = "5000"
    headers = {
        "X-BAPI-API-KEY": BYBIT_KEY,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
        "Content-Type": "application/json",
    }
    method = method.upper()
    if method == "GET":
        q = _qs(params)
        prehash = ts + BYBIT_KEY + recv + q
        sig = hmac.new(BYBIT_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        headers["X-BAPI-SIGN"] = sig
        url = f"{BYBIT_BASE}{path}" + (f"?{q}" if q else "")
        r = await _httpx_client.get(url, headers=headers)
    else:
        body = json.dumps(params, separators=(',', ':'), sort_keys=True)
        prehash = ts + BYBIT_KEY + recv + body
        sig = hmac.new(BYBIT_SECRET.encode(), prehash.encode(), hashlib.sha256).hexdigest()
        headers["X-BAPI-SIGN"] = sig
        url = f"{BYBIT_BASE}{path}"
        r = await _httpx_client.post(url, headers=headers, content=body.encode())

    try:
        data = r.json()
    except Exception:
        raise HTTPException(502, f"Bybit non-JSON response ({r.status_code}): {r.text[:200]}")

    if data.get("retCode") not in (0, 110043):
        print(f"❌ Bybit {method} {path} -> {data.get('retCode')} {data.get('retMsg')} :: {data}")

    if data.get("retCode") in (0, 110043):
        return data.get("result", {}) or {}

    raise HTTPException(502, f"Bybit error {data.get('retCode')}: {data.get('retMsg')}")

# ========= Symbol Filters =========
def _quantize(v: float, step: float, mode: str = "floor") -> float:
    if step <= 0: return v
    n = v / step
    if mode == "ceil":
        n = math.ceil(n - 1e-12)
    elif mode == "round":
        n = round(n)
    else:
        n = math.floor(n + 1e-12)
    return float(n * step)

async def get_symbol_meta(symbol: str) -> Dict[str, float]:
    if symbol in SYMBOL_META:
        return SYMBOL_META[symbol]
    res = await bybit("/v5/market/instruments-info", "GET", {
        "category": "linear", "symbol": symbol
    })
    lst = res.get("list", [])
    if not lst:
        raise HTTPException(400, f"Symbol not found/inactive: {symbol}")
    item = lst[0]
    pf  = item.get("priceFilter", {}) or {}
    lf  = item.get("lotSizeFilter", {}) or {}
    meta = {
        "tickSize": float(pf.get("tickSize", "0.01")),
        "stepSize": float(lf.get("qtyStep", lf.get("stepSize", "0.001"))),
        "minQty":   float(lf.get("minOrderQty", "0.001")),
        "minNotional": float(lf.get("minOrderAmt", "0")) if lf.get("minOrderAmt") else 0.0
    }
    SYMBOL_META[symbol] = meta
    print(f"ℹ️ {symbol} filters: {meta}")
    return meta

def quant_price(price: float, tick: float) -> float:
    return _quantize(price, tick, "round")

def quant_qty(qty: float, step: float) -> float:
    return _quantize(qty, step, "floor")

# ========= Wallet / Positions =========
async def positions(symbol: Optional[str] = None):
    params = {"category":"linear", "settleCoin": SETTLE_COIN}
    if symbol:
        params["symbol"] = symbol
    res = await bybit("/v5/position/list", "GET", params)
    return res.get("list", [])

async def positions_size_symbol(symbol: str) -> float:
    pos = await positions(symbol)
    if not pos: return 0.0
    return float(pos[0].get("size") or 0)

async def get_avg_entry_price(symbol: str) -> Optional[float]:
    ps = await positions(symbol)
    if not ps: return None
    try:
        v = float(ps[0].get("avgPrice") or 0.0)
        return v if v > 0 else None
    except:
        return None

async def count_open_filled() -> Dict[str,int]:
    res = await positions()
    longs = shorts = 0
    for p in res:
        sz = float(p.get("size") or 0)
        if sz <= 0: continue
        side = (p.get("side") or "").lower()
        if side == "buy": longs += 1
        elif side == "sell": shorts += 1
    return {"longs": longs, "shorts": shorts}

async def get_order_status(symbol: str, link_id: str) -> Optional[str]:
    try:
        res = await bybit("/v5/order/realtime", "GET", {
            "category": "linear", "symbol": symbol, "orderLinkId": link_id
        })
        lst = res.get("list", [])
        if lst: return lst[0].get("orderStatus")
    except: pass
    return None

# ========= Parser: klassisch =========
def parse_signals(text: str):
    txt = text.replace("\r","")
    blocks = re.split(r"\n\s*\n", txt.strip())
    signals = []
    for b in blocks:
        m_tf = re.search(r"Timeframe:\s*([A-Za-z0-9]+)", b, re.I)
        if not m_tf: continue
        tf = m_tf.group(1).upper()
        if tf not in ALLOWED_TFS: continue
        m_side = re.search(r"\b(BUY|SELL)\b", b, re.I)
        m_pair = re.search(r"on\s+([A-Z0-9]+)[/\-]([A-Z0-9]+)", b, re.I)
        m_entry= re.search(r"Price:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_tp1  = re.search(r"TP\s*1:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_tp2  = re.search(r"TP\s*2:\s*([0-9]*\.?[0-9]+)", b, re.I)
        m_sl   = re.search(r"\bSL\s*:\s*([0-9]*\.?[0-9]+)", b, re.I)
        if not (m_side and m_pair and m_entry and m_tp1 and m_tp2 and m_sl): continue
        side = "long" if m_side.group(1).upper()=="BUY" else "short"
        base, quote = m_pair.group(1).upper(), m_pair.group(2).upper()
        if quote=="USD": quote="USDT"
        entry,tp1,tp2,sl = map(float,[m_entry.group(1),m_tp1.group(1),m_tp2.group(1),m_sl.group(1)])
        if side=="long"  and not (sl < entry < tp1 <= tp2): continue
        if side=="short" and not (sl > entry > tp1 >= tp2): continue
        signals.append({"type":"classic","base":base,"quote":quote,"side":side,"entry":entry,"tps":[tp1,tp2],"sl":sl})
    return signals

# ========= Parser: Wickhunter/DCA =========
DCA_SIG_RE = re.compile(
    r"""
    ^\s*(?P<base>[A-Z0-9]+)\s+(?P<side>LONG|SHORT)\s+Signal\s*$
    .*?
    Enter\s+on\s+Trigger:\s*\$(?P<entry>[0-9]*\.?[0-9]+)\s*
    .*?
    TP1:\s*\$(?P<tp1>[0-9]*\.?[0-9]+)\s*
    TP2:\s*\$(?P<tp2>[0-9]*\.?[0-9]+)\s*
    TP3:\s*\$(?P<tp3>[0-9]*\.?[0-9]+)\s*
    .*?
    DCA\s*#1:\s*\$(?P<d1>[0-9]*\.?[0-9]+)\s*
    DCA\s*#2:\s*\$(?P<d2>[0-9]*\.?[0-9]+)\s*
    DCA\s*#3:\s*\$(?P<d3>[0-9]*\.?[0-9]+)\s*
    """,
    re.I | re.S | re.X
)

def parse_wickhunter_signal(text: str):
    txt = text.replace("\r", "")
    blocks = re.split(r"\n\s*\n", txt.strip())
    out = []
    for b in blocks:
        m = DCA_SIG_RE.search(b)
        if not m: 
            continue
        base = m.group("base").upper()
        side = "long" if m.group("side").upper() == "LONG" else "short"
        quote= "USDT"
        entry = float(m.group("entry"))
        tp1   = float(m.group("tp1"))
        tp2   = float(m.group("tp2"))
        tp3   = float(m.group("tp3"))
        d1    = float(m.group("d1"))
        d2    = float(m.group("d2"))
        d3    = float(m.group("d3"))
        if side=="long":
            if not (d1 < entry and d2 < d1 and d3 < d2 and entry < tp1 <= tp2 <= tp3):
                continue
        else:
            if not (d1 > entry and d2 > d1 and d3 > d2 and entry > tp1 >= tp2 >= tp3):
                continue
        out.append({
            "type":"wickhunter","base":base,"quote":quote,"side":side,
            "entry":entry,"tps":[tp1,tp2,tp3],"dcas":[d1,d2,d3]
        })
    return out

# ========= Helpers für TP-%-Abstände =========
def tp_distances_pct(entry: float, tps: List[float], side: str) -> List[float]:
    ds = []
    for tp in tps:
        if side == "long":
            ds.append((tp - entry) / entry * 100.0)
        else:
            ds.append((entry - tp) / entry * 100.0)
    return ds

def tps_from_avg(avg_entry: float, dists_pct: List[float], side: str) -> List[float]:
    new = []
    for d in dists_pct:
        if side == "long":
            new.append(avg_entry * (1 + d/100.0))
        else:
            new.append(avg_entry * (1 - d/100.0))
    return new

# ========= Orders / Leverage =========
def leverage_from_sl(entry: float, sl: float, side: str) -> int:
    if FIX_LEVERAGE > 0:
        return min(FIX_LEVERAGE, MAX_LEV_CAP)
    sl_pct = abs((entry-sl)/entry*100.0)
    lev = int(SAFETY_PCT // max(0.0001, sl_pct))
    return max(1, min(lev, MAX_LEV_CAP))

async def set_leverage(symbol: str, lev: int):
    await bybit("/v5/position/set-leverage","POST",{
        "category":"linear","symbol":symbol,
        "buyLeverage": str(lev),"sellLeverage": str(lev)
    })

def _alloc_notional(notional_base: float, scales: List[float]) -> List[float]:
    total_w = sum(scales)
    return [notional_base * (w/total_w) for w in scales]

async def place_limit(symbol: str, side: str, price: float, notional_usdt: float, leverage: int, link_prefix: str) -> Tuple[str, float]:
    meta = await get_symbol_meta(symbol)
    tick = meta["tickSize"]; step = meta["stepSize"]; minQty = meta["minQty"]
    px   = quant_price(price, tick)

    raw_qty = (notional_usdt * leverage) / max(px, 1e-9)
    qty = quant_qty(max(raw_qty, minQty), step)
    if qty < minQty:
        raise HTTPException(400, f"Qty too small after rounding for {symbol}. Increase notional or leverage.")

    BY = "Buy" if side=="long" else "Sell"
    uid = hex(int(now_ts()*1000))[2:]
    link_id = f"{link_prefix}_{symbol}_{uid}"

    await bybit("/v5/order/create","POST",{
        "category":"linear","symbol":symbol,"side":BY,
        "orderType":"Limit","price":str(px),"qty":str(qty),
        "timeInForce":"GTC","reduceOnly":False,"closeOnTrigger":False,
        "orderLinkId":link_id
    })
    print(f"📍 Limit placed {symbol} {BY} {qty} @ {px} ({link_id})")
    return link_id, qty

async def cancel_order(symbol: str, order_link_id: str):
    try:
        await bybit("/v5/order/cancel","POST",{
            "category":"linear","symbol":symbol,"orderLinkId":order_link_id
        })
        print(f"✅ Cancelled order: {order_link_id}")
    except Exception as e:
        print(f"⚠️ Cancel failed {order_link_id}: {e}")

async def cancel_many(symbol: str, link_ids: List[Optional[str]]):
    for lid in link_ids:
        if lid:
            await cancel_order(symbol, lid)

async def place_tp_sl_three(symbol: str, side: str, size: float, tp_prices: List[float], sl_price: float):
    meta = await get_symbol_meta(symbol)
    tick, step, minQty = meta["tickSize"], meta["stepSize"], meta["minQty"]

    tps_q = [quant_price(p, tick) for p in tp_prices]
    sl_q  = quant_price(sl_price, tick)

    raw_qs = [size * (p/100.0) for p in TP_THREE]
    qs = [quant_qty(q, step) for q in raw_qs]
    if sum(qs) > size:
        over = sum(qs) - size
        qs[-1] = quant_qty(max(qs[-1]-over, 0), step)

    OP = "Sell" if side=="long" else "Buy"
    uid = hex(int(now_ts()*1000))[2:]

    link_ids = []
    for i, (tp_px, q) in enumerate(zip(tps_q, qs), start=1):
        if q >= minQty and tp_px > 0:
            lid = f"tp{i}_{symbol}_{uid}"
            await bybit("/v5/order/create","POST",{
                "category":"linear","symbol":symbol,"side":OP,
                "orderType":"Limit","price":str(tp_px),"qty":str(q),
                "timeInForce":"GTC","reduceOnly":True,"closeOnTrigger":False,
                "orderLinkId":lid
            })
            print(f"✅ TP{i} {symbol} {OP} {q} @ {tp_px}")
            link_ids.append(lid)
        else:
            link_ids.append(None)
            print(f"ℹ️ TP{i} skipped (qty<{minQty} oder ungültiger Preis)")
    await bybit("/v5/position/set-trading-stop","POST",{
        "category":"linear","symbol":symbol,"tpSlMode":"Full","stopLoss": str(sl_q)
    })
    print(f"✅ SL set (position-wide) {symbol} @ {sl_q}")
    return link_ids, "pos_stop"

async def move_sl_to_be(symbol: str, side: str, entry_price: float):
    meta = await get_symbol_meta(symbol)
    be_q  = quant_price(entry_price, meta["tickSize"])
    off   = max(be_q * 0.0002, meta["tickSize"])  # winziger Offset
    be_px = be_q - off if side=="long" else be_q + off
    await bybit("/v5/position/set-trading-stop","POST",{
        "category":"linear","symbol":symbol,"tpSlMode":"Full",
        "stopLoss": str(quant_price(be_px, meta["tickSize"]))
    })
    print(f"✅ SL moved to BE {symbol} @ {be_px}")
    return "pos_stop_be"

# ========= Monitor Loop =========
def in_cooldown() -> bool:
    return (now_ts() - STATE.get("last_trade_ts",0.0)) < COOLDOWN_MIN*60

async def monitor_loop():
    print("🔄 Monitor loop started...")
    while True:
        await asyncio.sleep(5)
        to_del = []
        for symbol, meta in list(STATE["open_watch"].items()):
            try:
                # Ablauf Entry/DCA?
                if now_ts() - meta.get("created_ts", now_ts()) > meta.get("expiry_min", ENTRY_EXP_MIN)*60:
                    # cancelt ALLE offenen Entry/DCA, falls noch New/PartiallyFilled
                    for k in ("entry_id","dca1_id","dca2_id","dca3_id"):
                        lid = meta.get(k)
                        if not lid: continue
                        st = await get_order_status(symbol, lid)
                        if st in ("New","PartiallyFilled"):
                            await cancel_order(symbol, lid)
                    # wenn keine Position zustande kam -> entfernen
                    sz = await positions_size_symbol(symbol)
                    if sz == 0.0 and not meta.get("exits_set"):
                        print(f"🚫 Expired without fill: {symbol}")
                        to_del.append(symbol)
                        continue

                size = await positions_size_symbol(symbol)

                # Phase A: noch keine Exits gesetzt -> sobald Größe > 0: TP/SL setzen
                if size > 0 and not meta.get("exits_set"):
                    # %-Abstände für TPs aus Signal (Wickhunter) bzw. aus 2 TPs generieren
                    if meta["sig_type"] == "wickhunter":
                        dists = meta["tp_dists_pct"]  # vom Signal vorab berechnet
                        avg = await get_avg_entry_price(symbol) or meta["entry_px"]
                        new_tps = tps_from_avg(avg, dists, meta["side"])
                        # SL relativ zu DCA3:
                        d3 = meta["dcas"][2]
                        sl = d3 * (1 - SL_OVER_DCA3_PCT/100.0) if meta["side"]=="long" else d3 * (1 + SL_OVER_DCA3_PCT/100.0)
                        tp_links, sl_id = await place_tp_sl_three(symbol, meta["side"], size, new_tps, sl)
                        meta.update({"tp_links": tp_links, "sl_id": sl_id, "exits_set": True, "tp1_hit": False, "sl_be": False})
                        STATE["last_trade_ts"] = now_ts()  # Cooldown startet erst jetzt
                    else:
                        # klassisch: 2 TPs -> wir setzen einfach TP1/TP2 (altes Schema), SL wie geliefert
                        # konvertiere auf 3er-Handler mit [tp1,tp2,tp2] und Splits (TP3 kann 0 sein)
                        d1, d2 = meta["tps"][0], meta["tps"][1]
                        tps = [d1, d2, d2]
                        tp_links, sl_id = await place_tp_sl_three(symbol, meta["side"], size, tps, meta["sl_px"])
                        meta.update({"tp_links": tp_links, "sl_id": sl_id, "exits_set": True, "tp1_hit": False, "sl_be": False})
                        STATE["last_trade_ts"] = now_ts()
                    continue

                # Phase B: TP1 fill -> SL auf BE
                if meta.get("exits_set") and not meta.get("tp1_hit"):
                    l0 = (meta.get("tp_links") or [None,None,None])[0]
                    if l0:
                        st_tp1 = await get_order_status(symbol, l0)
                        if st_tp1 == "Filled":
                            meta["tp1_hit"] = True
                            if not meta.get("sl_be"):
                                await move_sl_to_be(symbol, meta["side"], meta["initial_entry_for_be"])
                                meta["sl_be"] = True

                # Phase C: DCA-Fills -> TPs repricen
                dca_filled = False
                for key in ("dca1_id","dca2_id","dca3_id"):
                    lid = meta.get(key)
                    if not lid: continue
                    st = await get_order_status(symbol, lid)
                    # Sobald eine DCA gefüllt wurde, repricen wir TPs auf Basis des neuen Avg
                    if st == "Filled" and key not in meta.get("repriced_for", set()):
                        meta.setdefault("repriced_for", set()).add(key)
                        dca_filled = True

                if dca_filled and meta.get("exits_set"):
                    # alte TPs canceln
                    await cancel_many(symbol, meta.get("tp_links", []))
                    # neue Preise:
                    if meta["sig_type"] == "wickhunter":
                        avg = await get_avg_entry_price(symbol) or meta["entry_px"]
                        new_tps = tps_from_avg(avg, meta["tp_dists_pct"], meta["side"])
                        # SL bleibt relativ zu DCA3 konstant
                        d3 = meta["dcas"][2]
                        sl = d3 * (1 - SL_OVER_DCA3_PCT/100.0) if meta["side"]=="long" else d3 * (1 + SL_OVER_DCA3_PCT/100.0)
                        size = await positions_size_symbol(symbol)
                        tp_links, sl_id = await place_tp_sl_three(symbol, meta["side"], size, new_tps, sl)
                        meta.update({"tp_links": tp_links, "sl_id": sl_id})

                # Phase D: Cleanup wenn Position zu
                cur = await positions_size_symbol(symbol)
                if cur == 0.0 and meta.get("exits_set"):
                    to_del.append(symbol)
                    print(f"🏁 Position closed {symbol}")

            except Exception as e:
                print(f"❌ Monitor error {symbol}: {e}")

        for s in to_del:
            STATE["open_watch"].pop(s, None)

# ========= Guards / Helpers =========
def extract_text_from_payload(payload: dict) -> str:
    node = payload
    for part in TEXT_PATH.split("."):
        if isinstance(node, dict) and part in node: node = node[part]
        else: return ""
    return node if isinstance(node, str) else ""

# ========= HTTP Endpoints =========
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    text = (body.get("text") or extract_text_from_payload(body)).strip()
    if not text:
        raise HTTPException(400, "No signal text")

    # 1) Wickhunter/DCA zuerst versuchen
    wh = parse_wickhunter_signal(text)
    if wh:
        sig = wh[0]
        base, quote, side = sig["base"], sig["quote"], sig["side"]
        entry, tps, dcas = sig["entry"], sig["tps"], sig["dcas"]
        symbol = f"{base}{quote}"

        print(f"📨 Wickhunter: {symbol} {side} Entry:{entry} TP1-3:{tps} DCA:{dcas}")

        # Limits offene Positionen (nur gefüllte)
        counts = await count_open_filled()
        if side == "long" and counts["longs"] >= MAX_OPEN_LONGS:
            raise HTTPException(429, f"Max open longs reached ({counts['longs']}/{MAX_OPEN_LONGS})")
        if side == "short" and counts["shorts"] >= MAX_OPEN_SHORTS:
            raise HTTPException(429, f"Max open shorts reached ({counts['shorts']}/{MAX_OPEN_SHORTS})")
        if in_cooldown():
            raise HTTPException(429, f"In cooldown ({COOLDOWN_MIN} min since last fill)")

        # Hebel – fest oder dynamisch
        # für dynamisch bräuchten wir SL; wir verwenden hier SL aus DCA3
        sl_from_d3 = dcas[2]*(1 - SL_OVER_DCA3_PCT/100.0) if side=="long" else dcas[2]*(1 + SL_OVER_DCA3_PCT/100.0)
        lev = leverage_from_sl(entry, sl_from_d3, side)
        await set_leverage(symbol, lev)

        # Notional-Verteilung auf 4 Tranchen (Initial + DCA1..3)
        notional_base = float(body.get("notional") or DEFAULT_NOTIONAL)
        parts = _alloc_notional(notional_base, DCA_SCALES)

        # Entry + DCAs als Limits
        entry_id, qty0 = await place_limit(symbol, side, entry, parts[0], lev, "ent")
        dca1_id, _ = await place_limit(symbol, side, dcas[0], parts[1], lev, "dca1")
        dca2_id, _ = await place_limit(symbol, side, dcas[1], parts[2], lev, "dca2")
        dca3_id, _ = await place_limit(symbol, side, dcas[2], parts[3], lev, "dca3")

        # %-Abstände der TP aus Entry (für späteres Repricing)
        dists = tp_distances_pct(entry, tps, side)

        STATE["open_watch"][symbol] = {
            "sig_type":"wickhunter",
            "entry_id": entry_id, "dca1_id": dca1_id, "dca2_id": dca2_id, "dca3_id": dca3_id,
            "side": side, "entry_px": entry, "initial_entry_for_be": entry,
            "tps": tps, "tp_dists_pct": dists, "dcas": dcas,
            "created_ts": now_ts(), "expiry_min": ENTRY_EXP_MIN,
            "exits_set": False, "tp1_hit": False, "sl_be": False
        }

        return {
            "ok": True, "format":"wickhunter", "symbol": symbol, "side": side,
            "entry": entry, "tps": tps, "dcas": dcas,
            "leverage": lev, "notional_base": notional_base,
            "splits_tp": TP_THREE, "dca_scales": DCA_SCALES
        }

    # 2) klassisches Format fallback
    sigs = parse_signals(text)
    if not sigs:
        raise HTTPException(422, f"No valid signal (neither Wickhunter nor classic for TFs {sorted(ALLOWED_TFS)})")

    sig = sigs[0]
    base, quote, side = sig["base"], sig["quote"], sig["side"]
    entry, tps, sl = sig["entry"], sig["tps"], sig["sl"]
    symbol = f"{base}{quote}"

    print(f"📨 Classic: {symbol} {side} Entry:{entry} TP1-2:{tps} SL:{sl}")

    counts = await count_open_filled()
    if side == "long" and counts["longs"] >= MAX_OPEN_LONGS:
        raise HTTPException(429, f"Max open longs reached ({counts['longs']}/{MAX_OPEN_LONGS})")
    if side == "short" and counts["shorts"] >= MAX_OPEN_SHORTS:
        raise HTTPException(429, f"Max open shorts reached ({counts['shorts']}/{MAX_OPEN_SHORTS})")
    if in_cooldown():
        raise HTTPException(429, f"In cooldown ({COOLDOWN_MIN} min since last fill)")

    lev = leverage_from_sl(entry, sl, side)
    await set_leverage(symbol, lev)

    notional = float(body.get("notional") or DEFAULT_NOTIONAL)
    entry_id, qty = await place_limit(symbol, side, entry, notional, lev, "ent")

    STATE["open_watch"][symbol] = {
        "sig_type":"classic",
        "entry_id": entry_id, "side": side,
        "entry_px": entry, "initial_entry_for_be": entry,
        "tps": tps, "sl_px": sl,
        "created_ts": now_ts(), "expiry_min": ENTRY_EXP_MIN,
        "exits_set": False, "tp1_hit": False, "sl_be": False
    }

    return {
        "ok": True, "format":"classic", "symbol": symbol, "side": side,
        "entry": entry, "tps": tps, "sl": sl,
        "leverage": lev, "notional": notional, "entry_qty": qty
    }

@app.get("/health")
async def health():
    return {"ok": True, "watch": list(STATE["open_watch"].keys())}
