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

def analyze_structure(data, is_buy_signal, tf_name) -> tuple[bool, str]:
    if not isinstance(data, list) or len(data) < 4: 
        return False, f"No data ({tf_name})"
        
    bull_vol = 0.0
    bear_vol = 0.0
    candles = []
    
    for c in data:
        o, h, l, close, v = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])
        candles.append({"open": o, "high": h, "low": l, "close": close, "vol": v})
        if close > o: bull_vol += v
        else: bear_vol += v
        
    if is_buy_signal:
        if candles[3]["low"] >= candles[1]["low"] * 0.999: # HL o plano
            if bull_vol > bear_vol: return True, "Alcista (HL) + Vol. Compra"
            else: return False, "Sin Vol. Comprador"
        else: return False, "Rompiendo a la Baja"
    else:
        if candles[3]["high"] <= candles[1]["high"] * 1.001: # LH o plano
            if bear_vol > bull_vol: return True, "Bajista (LH) + Vol. Venta"
            else: return False, "Sin Vol. Vendedor"
        else: return False, "Rompiendo al Alza"

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

async def fetch_kline(session, url):
    async with session.get(url, timeout=5) as resp:
        return await resp.json()

async def get_multiframe_context(symbol: str, is_buy_signal: bool):
    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=3m&limit=4",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=4",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=4",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=288" # Dia entero
    ]
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            tasks = [fetch_kline(session, u) for u in urls]
            res_3m, res_5m, res_15m, res_vwap = await asyncio.gather(*tasks)
            
            align_3m, msg_3m = analyze_structure(res_3m, is_buy_signal, "3m")
            align_5m, msg_5m = analyze_structure(res_5m, is_buy_signal, "5m")
            align_15m, msg_15m = analyze_structure(res_15m, is_buy_signal, "15m")
            vwap, stdev = calculate_vwap(res_vwap)
            
            return align_3m, msg_3m, align_5m, msg_5m, align_15m, msg_15m, vwap, stdev
    except asyncio.TimeoutError:
        return False, "Timeout", False, "Timeout", False, "Timeout", None, None
    except Exception as e:
        print(f"[!] Error multiframe: {e}")
        return False, "Error API", False, "Error API", False, "Error API", None, None
            
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
    
    # Check 3m/5m/15m and VWAP
    align_3m, msg_3m, align_5m, msg_5m, align_15m, msg_15m, vwap, stdev = await get_multiframe_context(SYMBOL.upper(), is_buy_signal)
    
    # Determine the context verdict
    verdict = ""
    prob_score = 0
    total_score = 8 # Spotlight on 8 dynamic elements (Spot, Fut, OI, Depth, POC, 3m/5m/15m Align)
    
    # Checkmark display states
    spot_check = "❌"
    fut_check = "❌"
    oi_check = "❌"
    depth_check = "❌"
    poc_liq_check = "❌"
    ms_3_check = "✅" if align_3m else "❌"
    ms_5_check = "✅" if align_5m else "❌"
    ms_15_check = "✅" if align_15m else "❌"
    
    if align_3m: prob_score += 1
    if align_5m: prob_score += 1
    if align_15m: prob_score += 1
    
    vwap_msg = "No data"
    if vwap:
        if is_buy_signal and ctx.price > vwap:
            vwap_msg = f"✅ Precio ARRIBA del VWAP (${vwap:,.1f})"
        elif is_sell_signal and ctx.price < vwap:
            vwap_msg = f"✅ Precio DEBAJO del VWAP (${vwap:,.1f})"
        else:
            vwap_msg = f"❌ Precio contra el VWAP (${vwap:,.1f})"
            
        # Optional: Check if resting on deviation bands
        lower_band1, upper_band1 = vwap - stdev, vwap + stdev
        vwap_msg += f" [Desviación: ±${stdev:,.0f}]"
    
    if is_buy_signal:
        if ctx.spot_cvd > 0: 
            prob_score += 1     # +Spot buying
            spot_check = "✅"
        if ctx.futures_cvd > 0: 
            prob_score += 1  # +Futures buying
            fut_check = "✅"
        if oi_delta_pct > 0: 
            prob_score += 1     # +OI rising (new longs)
            oi_check = "✅"
        if ctx.depth_0_5_delta_usd > 10000000:   # Mínimo +$10M netos hacia arriba (0-5%)
            prob_score += 1                      # +Heatmap Bid protection barrier
            depth_check = "✅"
        if long_liqs > 500000 or (ctx.session_poc_price > 0 and ctx.price > ctx.session_poc_price):
            prob_score += 1                      # +Liquidity Sweep or Above POC
            poc_liq_check = "✅"
            
        if prob_score >= 6:
            verdict = "🔥 ALTA PROBABILIDAD (Confirmado por Order Flow Integral)"
        elif prob_score >= 4:
            verdict = "⚠️ PROBABILIDAD MEDIA (Fuerzas Divididas)"
        else:
            verdict = "❌ BAJA PROBABILIDAD (Order Flow en Contra)"
            
    elif is_sell_signal:
        if ctx.spot_cvd < 0: 
            prob_score += 1     # +Spot selling
            spot_check = "✅"
        if ctx.futures_cvd < 0: 
            prob_score += 1  # +Futures selling
            fut_check = "✅"
        if oi_delta_pct > 0: 
            prob_score += 1     # +OI rising (new shorts)
            oi_check = "✅"
        if ctx.depth_0_5_delta_usd < -10000000:  # Mínimo -$10M netos hacia abajo (0-5%)
            prob_score += 1                      # +Heatmap Ask resistance barrier
            depth_check = "✅"
        if short_liqs > 500000 or (0 < ctx.price < ctx.session_poc_price):
            prob_score += 1                      # +Liquidity Sweep or Below POC
            poc_liq_check = "✅"
            
        if prob_score >= 6:
            verdict = "🔥 ALTA PROBABILIDAD (Confirmado por Order Flow Integral)"
        elif prob_score >= 4:
            verdict = "⚠️ PROBABILIDAD MEDIA (Fuerzas Divididas)"
        else:
            verdict = "❌ BAJA PROBABILIDAD (Order Flow en Contra)"
    else:
        verdict = "Señal Neutral"
            
    delta_sign = "+" if ctx.depth_0_5_delta_usd > 0 else ""
    
    wall_str = "Ninguno"
    if ctx.heatmap_walls:
        best_wall = ctx.heatmap_walls[0]
        wall_str = f"{best_wall[1]:.0f} BTC en ${best_wall[0]:,.0f} ({best_wall[2]})"
            
    # Always format the complete message so the user can make the final decision
    alert_text = (
        f"🚨 <b>SEÑAL TRADINGVIEW</b> 🚨\n\n"
        f"<b>Par:</b> {alert.pair} ({alert.timeframe})\n"
        f"<b>Señal:</b> {alert.signal}\n"
        f"<b>Precio Actual:</b> ${alert.price:,.2f}\n"
        f"<b>Veredicto Bot:</b> <b>{verdict}</b> ({prob_score}/{total_score})\n\n"
        f"📊 <b>CONTEXTO DE MERCADO EN VIVO</b>\n"
        f"├─ [{poc_liq_check}] <b>POC Sesión:</b> ${ctx.session_poc_price:,.2f}\n"
        f"├─ <b>VWAP:</b> {vwap_msg}\n"
        f"├─ [{ms_15_check}] <b>Estructura 15m (Macro):</b> {msg_15m}\n"
        f"├─ [{ms_5_check}] <b>Estructura 5m (Principal):</b> {msg_5m}\n"
        f"├─ [{ms_3_check}] <b>Estructura 3m (Gatillo):</b> {msg_3m}\n"
        f"├─ [{spot_check}] <b>CVD Spot:</b> ${ctx.spot_cvd:,.0f}\n"
        f"├─ [{fut_check}] <b>CVD Futuros:</b> ${ctx.futures_cvd:,.0f}\n"
        f"├─ [{oi_check}] <b>Delta OI (5m):</b> {oi_delta_pct:+.3f}%\n"
        f"├─ [{depth_check}] <b>Delta Profundidad (0-5%):</b> {delta_sign}${ctx.depth_0_5_delta_usd:,.0f}\n"
        f"├─ <b>🐋 Muro Heatmap (>500):</b> {wall_str}\n"
        f"└─ <b>Liqs Recientes:</b> L: ${long_liqs:,.0f} | S: ${short_liqs:,.0f}\n"
    )
    
    if alert.optional_msg:
        alert_text += f"\n<i>Nota: {alert.optional_msg}</i>"
        
    asyncio.create_task(send_telegram_message(alert_text))
    return {"status": "success", "verdict": verdict}

@app.get("/")
def read_root():
    return {"status": "online", "service": "High-Probability Engine"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
