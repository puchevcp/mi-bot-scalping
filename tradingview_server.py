from fastapi import FastAPI, Request
import uvicorn
from pydantic import BaseModel
from typing import Optional
from telegram_notifier import send_telegram_message
import asyncio
import aiohttp

# Import the existing variables and start functions from our data engine
from binance_data import (
    ctx, listen_trades, SPOT_WS_URL, FUTURES_WS_URL, SYMBOL,
    listen_local_orderbook, listen_liquidations, fetch_oi_loop, display_context
)

app = FastAPI(title="High-Probability Webhook Engine")

class TVAlert(BaseModel):
    pair: str
    timeframe: str
    signal: str
    price: float
    optional_msg: str = ""

@app.on_event("startup")
async def startup_event():
    # Start the Binance data engines in the background when the server starts
    print("[*] Iniciando Motores de Order Flow Avanzados...")
    asyncio.create_task(listen_trades(SPOT_WS_URL, is_spot=True))
    asyncio.create_task(listen_trades(FUTURES_WS_URL, is_spot=False))
    asyncio.create_task(listen_local_orderbook())
    asyncio.create_task(listen_liquidations())
    asyncio.create_task(fetch_oi_loop())
    asyncio.create_task(display_context())
import math
from datetime import datetime, timezone

def analyze_structure(data, is_buy_signal, tf_name) -> tuple[bool, str, float]:
    if not isinstance(data, list) or len(data) < 11: 
        return False, f"Pocos datos ({tf_name})", 1.0
        
    # Extraer ultimas 4 velas para la estructura
    struct_candles = data[-4:]
    bull_vol = 0.0
    bear_vol = 0.0
    candles = []
    
    for c in struct_candles:
        o, h, l, close, v = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])
        candles.append({"open": o, "high": h, "low": l, "close": close, "vol": v})
        if close > o: bull_vol += v
        else: bear_vol += v
        
    # Calcular RVOL (Volumen de la ultima vela vs promedio de las 10 anteriores)
    vol_actual = candles[-1]["vol"]
    vols_hist = [float(c[5]) for c in data[-11:-1]]
    vol_promedio = sum(vols_hist) / len(vols_hist)
    rvol = vol_actual / vol_promedio if vol_promedio > 0 else 1.0
    
    r_ok = (rvol >= 1.2)
    rvol_tag = f" ⭐ (RVOL {rvol:.1f})" if r_ok else f" (RVOL {rvol:.1f})"
    
    if is_buy_signal:
        if candles[3]["low"] >= candles[1]["low"] * 0.999: # HL o plano
            if bull_vol > bear_vol: return True, f"Alcista (HL) + Vol. Compra{rvol_tag}", rvol
            else: return False, f"Sin Vol. Comprador{rvol_tag}", rvol
        else: return False, f"Rompiendo a la Baja{rvol_tag}", rvol
    else:
        if candles[3]["high"] <= candles[1]["high"] * 1.001: # LH o plano
            if bear_vol > bull_vol: return True, f"Bajista (LH) + Vol. Venta{rvol_tag}", rvol
            else: return False, f"Sin Vol. Vendedor{rvol_tag}", rvol
        else: return False, f"Rompiendo al Alza{rvol_tag}", rvol

def calculate_vwap(data):
    if not isinstance(data, list): return None, None
    now_utc = datetime.now(timezone.utc)
    start_of_day_ts = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc).timestamp() * 1000
    
    todays_candles = [c for c in data if c[0] >= start_of_day_ts]
    if not todays_candles: return None, None
    
    cum_vol = 0.0
    cum_vol_price = 0.0
    for c in todays_candles:
        h, l, close, vol = float(c[2]), float(c[3]), float(c[4]), float(c[5])
        typ_price = (h + l + close) / 3.0
        cum_vol += vol
        cum_vol_price += typ_price * vol
        
    vwap = cum_vol_price / cum_vol if cum_vol > 0 else 0
    
    variances = []
    for c in todays_candles:
        h, l, close, vol = float(c[2]), float(c[3]), float(c[4]), float(c[5])
        typ_price = (h + l + close) / 3.0
        variances.append((vol / cum_vol) * ((typ_price - vwap) ** 2))
        
    stdev = math.sqrt(sum(variances))
    return vwap, stdev

def calculate_atr(data, period=14):
    if not isinstance(data, list) or len(data) < period + 1: return 0
    tr_list = []
    for i in range(1, len(data)):
        h, l, prev_c = float(data[i][2]), float(data[i][3]), float(data[i-1][4])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def calculate_mfi(data, period=60):
    if not isinstance(data, list) or len(data) < period + 1: return None, None
    
    # Formula VMC: SMA(((Close - Open) / (High - Low)) * 150, period) - 2.5
    mfi_values = []
    for c in data:
        o, h, l, close = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        # Evitar division por cero si high == low
        raw_mfi = (((close - o) / (h - l)) * 150) if (h - l) != 0 else 0
        mfi_values.append(raw_mfi - 2.5)
    
    # Calcular SMA de los ultimos 'period' valores para el actual y el anterior
    current_mfi_sma = sum(mfi_values[-period:]) / period
    prev_mfi_sma = sum(mfi_values[-(period+1):-1]) / period
    
    return current_mfi_sma, prev_mfi_sma

def calculate_wt(data, chlen=12, avglen=3):
    if not isinstance(data, list) or len(data) < 20: return 0, 0
    df = pd.DataFrame(data, columns=["ts","open","high","low","close","vol"])
    for col in ["open","high","low","close","vol"]: df[col] = df[col].astype(float)
    
    hlc3 = (df['high'] + df['low'] + df['close']) / 3.0
    esa = hlc3.ewm(span=chlen, adjust=False).mean()
    de = (hlc3 - esa).abs().ewm(span=chlen, adjust=False).mean()
    ci = (hlc3 - esa) / (0.015 * de)
    wt1 = ci.ewm(span=avglen, adjust=False).mean()
    wt2 = wt1.rolling(window=4).mean()
    return wt1.iloc[-1], wt2.iloc[-1]

import pandas as pd

async def fetch_kline(session, url):
    async with session.get(url, timeout=5) as resp:
        return await resp.json()

async def get_multiframe_context(symbol: str, is_buy_signal: bool):
    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=3m&limit=100", # Mas velas para ATR
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=20",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=20",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=288" # Dia entero
    ]
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            tasks = [fetch_kline(session, u) for u in urls]
            res_3m, res_5m, res_15m, res_vwap = await asyncio.gather(*tasks)
            
            align_3m, msg_3m, r3 = analyze_structure(res_3m, is_buy_signal, "3m")
            align_5m, msg_5m, r5 = analyze_structure(res_5m, is_buy_signal, "5m")
            align_15m, msg_15m, r15 = analyze_structure(res_15m, is_buy_signal, "15m")
            vwap, stdev = calculate_vwap(res_vwap)
            mfi_now, mfi_prev = calculate_mfi(res_vwap)
            
            # ATR de 3m para objetivos dinamicos
            atr_3m = calculate_atr(res_3m)
            
            # Calcuar WT1 base para filtrado Sniper (usando 3m ahora como base Platinum)
            wt1_val, wt2_val = calculate_wt(res_3m)
            
            return align_3m, msg_3m, align_5m, msg_5m, align_15m, msg_15m, vwap, stdev, mfi_now, mfi_prev, r3, wt1_val, atr_3m
    except asyncio.TimeoutError:
        return False, "Timeout", False, "Timeout", False, "Timeout", None, None, None, None, 1.0, 0, 0
    except Exception as e:
        print(f"[!] Error multiframe: {e}")
        return False, "Error API", False, "Error API", False, "Error API", None, None, None, None, 1.0, 0, 0
            
@app.post("/webhook")
async def receive_webhook(request: Request):
    alert = TVAlert(**await request.json())
    print(f"\n[!] Webhook recibido: {alert.signal} en {alert.pair}")
    
    # Analyze High-Probability Context
    # Example logic for a "BUY" signal
    is_buy_signal = "COMPRA" in alert.signal.upper() or "BUY" in alert.signal.upper()
    is_sell_signal = "VENTA" in alert.signal.upper() or "SELL" in alert.signal.upper()
    
    # Calculate OI delta
    oi_delta_pct = 0.0
    if ctx.oi_5m_ago > 0:
        oi_delta_pct = ((ctx.oi_current - ctx.oi_5m_ago) / ctx.oi_5m_ago) * 100
        
    # Liquidations
    long_liqs = sum(v for t, l, v in ctx.recent_liquidations if l == "LONG")
    short_liqs = sum(v for t, l, v in ctx.recent_liquidations if l == "SHORT")
       # Check 3m/5m/15m and VWAP/MFI
    (align_3m, msg_3m, align_5m, msg_5m, align_15m, msg_15m, 
     vwap, stdev, mfi_now, mfi_prev, rvol_3m, wt1_3m, atr_3m) = await get_multiframe_context(SYMBOL.upper(), is_buy_signal)
    
    # Determine the context verdict
    verdict = ""
    prob_score = 0
    total_score = 10 
    
    # Platinum 3m Logic (Derived from Phase 12 Backtest)
    is_platinum = False
    mfi_slope_ok = False
    if mfi_now is not None and mfi_prev is not None:
        if is_buy_signal and mfi_now > mfi_prev: mfi_slope_ok = True
        if is_sell_signal and mfi_now < mfi_prev: mfi_slope_ok = True
    
    # Golden Zone Check (3m base)
    loc_ok = False
    if is_buy_signal and -65 < wt1_3m < -35: loc_ok = True
    if is_sell_signal and 35 < wt1_3m < 65: loc_ok = True
    
    if align_3m and align_15m and mfi_slope_ok and loc_ok and rvol_3m >= 1.2:
        is_platinum = True
    
    # Calculate Dynamic ATR Targets (Percentage)
    # TP1 (Safe) = 1.0x ATR | TP2 (Platinum) = 1.5x ATR | TP3 (Moon) = 2.0x ATR
    tp1_pct = (atr_3m / alert.price) * 100 if alert.price > 0 else 0
    tp2_pct = (1.5 * atr_3m / alert.price) * 100 if alert.price > 0 else 0
    tp3_pct = (2.0 * atr_3m / alert.price) * 100 if alert.price > 0 else 0
    
    # Checkmark display states
    spot_check = "❌"
    fut_check = "❌"
    oi_check = "❌"
    ms_3_check = "✅" if align_3m else "❌"
    ms_15_check = "✅" if align_15m else "❌"
    mfi_check = "✅" if mfi_slope_ok else "❌"
    
    if align_3m: prob_score += 1
    if align_5m: prob_score += 1
    if align_15m: prob_score += 1
    
    vwap_msg = "No data"
    if vwap:
        if (is_buy_signal and ctx.price > vwap) or (is_sell_signal and ctx.price < vwap):
            vwap_msg = f"✅ Precio ARRIBA del VWAP" if is_buy_signal else f"✅ Precio DEBAJO del VWAP"
        else:
            vwap_msg = f"❌ Precio contra el VWAP"
        
    mfi_msg = f"{mfi_now:+.1f} ({'📈' if mfi_now > mfi_prev else '📉'})" if mfi_now else "No data"
    
    # Score other OF components
    if is_buy_signal:
        if ctx.spot_cvd > 0: prob_score += 2; spot_check = "✅"
        if ctx.futures_cvd > 0: prob_score += 2; fut_check = "✅"
        if oi_delta_pct > 0: prob_score += 1; oi_check = "✅"
    elif is_sell_signal:
        if ctx.spot_cvd < 0: prob_score += 2; spot_check = "✅"
        if ctx.futures_cvd < 0: prob_score += 2; fut_check = "✅"
        if oi_delta_pct > 0: prob_score += 1; oi_check = "✅"

    # Verdict Logic
    if is_platinum:
        verdict = "💎 <b>SNIPER PLATINUM (63%+ Prob)</b>"
        prob_score = 10 
    elif not align_15m:
        verdict = "⚠️ <b>RIESGO DE TRAMPA (Contra 15m)</b>"
        prob_score = min(prob_score, 5)
    elif align_3m and prob_score >= 7:
        verdict = "🔥 <b>ALTA PROBABILIDAD (Confirmado)</b>"
    else:
        verdict = "⚖️ <b>Buscando Confirmación</b>"
            
    wall_str = "Ninguno"
    if ctx.heatmap_walls:
        best_wall = ctx.heatmap_walls[0]
        wall_str = f"{best_wall[1]:.0f} BTC en ${best_wall[0]:,.0f}"
            
    # Message Construction
    alert_text = (
        f"🚨 <b>SNIPER ELITE {alert.timeframe}</b> 🚨\n\n"
        f"<b>Señal:</b> {alert.signal} (${alert.price:,.2f})\n"
        f"<b>Veredicto:</b> {verdict} ({prob_score}/10)\n\n"
        f"🎯 <b>OBJETIVOS SUGERIDOS (ATR Dynamic)</b>\n"
        f"├─ 🟢 <b>TP 1 (Safe):</b> +{tp1_pct:.2f}% (${(alert.price * (1 + tp1_pct/100) if is_buy_signal else alert.price * (1 - tp1_pct/100)):,.1f})\n"
        f"├─ 💎 <b>TP 2 (Platinum):</b> +{tp2_pct:.2f}% (${(alert.price * (1 + tp2_pct/100) if is_buy_signal else alert.price * (1 - tp2_pct/100)):,.1f})\n"
        f"└─ 🚀 <b>TP 3 (Moon):</b> +{tp3_pct:.2f}% (${(alert.price * (1 + tp3_pct/100) if is_buy_signal else alert.price * (1 - tp3_pct/100)):,.1f})\n"
        f"   <i>SL recomendado: -0.25% fijos.</i>\n\n"
        f"📊 <b>ORDEN FLOW & ESTRUCTURA</b>\n"
        f"├─ [{ms_15_check}] <b>Estructura 15m (Macro):</b> {msg_15m}\n"
        f"├─ [{ms_3_check}] <b>Estructura 3m (Principal):</b> {msg_3m}\n"
        f"├─ [{'✅' if loc_ok else '❌'}] <b>Zona W.T (Golden):</b> {wt1_3m:+.1f}\n"
        f"├─ [{mfi_check}] <b>MFI Flow:</b> {mfi_msg}\n"
        f"├─ [{spot_check}] <b>CVD Spot:</b> ${ctx.spot_cvd:,.0f}\n"
        f"├─ [{fut_check}] <b>CVD Futuros:</b> ${ctx.futures_cvd:,.0f}\n"
        f"├─ [{oi_check}] <b>Delta OI (5m):</b> {oi_delta_pct:+.3f}%\n"
        f"└─ <b>Muro Heatmap:</b> {wall_str}\n"
    )
    
    if alert.optional_msg:
        alert_text += f"\n<i>Nota: {alert.optional_msg}</i>"
        
    asyncio.create_task(send_telegram_message(alert_text))
    return {"status": "success", "verdict": verdict}

@app.get("/")
def read_root():
    return {"status": "online", "service": "Platinum Sniper Engine"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
