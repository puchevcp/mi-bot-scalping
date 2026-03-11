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

async def check_5m_structure(is_buy_signal: bool) -> tuple[bool, str]:
    """ Fetch last 4 5-minute candles to check short-term trend and volume. """
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={SYMBOL.upper()}&interval=5m&limit=4"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as resp:
                data = await resp.json()
                if len(data) < 4: return False, "No data"
                
                # data = [ [Open time, Open, High, Low, Close, Volume, Close time, ...], ... ]
                candles = []
                bull_vol = 0.0
                bear_vol = 0.0
                
                for candle in data:
                    o = float(candle[1])
                    h = float(candle[2])
                    l = float(candle[3])
                    c = float(candle[4])
                    v = float(candle[5])
                    candles.append({"open": o, "high": h, "low": l, "close": c, "vol": v})
                    
                    if c > o: bull_vol += v
                    else: bear_vol += v
                
                # Check Market Structure (Higher Lows for BUY, Lower Highs for SELL)
                msg = ""
                is_aligned = False
                
                if is_buy_signal:
                    # Check if the lows are generally stepping up or flat, not dumping
                    if candles[3]["low"] >= candles[1]["low"] * 0.999: # Allow tiny wick tolerance
                        if bull_vol > bear_vol:
                            is_aligned = True
                            msg = "Estructura Alcista (HL) + Vol. Comprador"
                        else:
                            msg = "Sin Vol. Comprador"
                    else:
                        msg = "Estructura 5m Rompiendo a la Baja"
                else:
                    # Check if the highs are stepping down or flat
                    if candles[3]["high"] <= candles[1]["high"] * 1.001:
                        if bear_vol > bull_vol:
                            is_aligned = True
                            msg = "Estructura Bajista (LH) + Vol. Vendedor"
                        else:
                            msg = "Sin Vol. Vendedor"
                    else:
                        msg = "Estructura 5m Rompiendo al Alza"
                        
                return is_aligned, msg
        except Exception as e:
            return False, f"Error API 5m"
            
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
    
    # Check 5m Market Structure
    ms_aligned, ms_msg = await check_5m_structure(is_buy_signal)
    
    # Determine the context verdict
    verdict = ""
    prob_score = 0
    total_score = 6 # Added 5m Structure check
    
    # Checkmark display states
    spot_check = "❌"
    fut_check = "❌"
    oi_check = "❌"
    depth_check = "❌"
    poc_liq_check = "❌"
    ms_check = "✅" if ms_aligned else "❌"
    
    if ms_aligned:
        prob_score += 1
    
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
            
        if prob_score >= 5:
            verdict = "🔥 ALTA PROBABILIDAD (Confirmado por Order Flow Integral)"
        elif prob_score >= 3:
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
            
        if prob_score >= 5:
            verdict = "🔥 ALTA PROBABILIDAD (Confirmado por Order Flow Integral)"
        elif prob_score >= 3:
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
        f"├─ [{ms_check}] <b>Estructura 5m:</b> {ms_msg}\n"
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
