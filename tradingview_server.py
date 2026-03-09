from fastapi import FastAPI
import uvicorn
from pydantic import BaseModel
from telegram_notifier import send_telegram_message
import asyncio

# Import the existing variables and start functions from our data engine
from binance_data import (
    ctx, listen_trades, SPOT_WS_URL, FUTURES_WS_URL, 
    listen_book_ticker, fetch_oi_loop, display_context
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
    print("[*] Iniciando Motores de Order Flow...")
    asyncio.create_task(listen_trades(SPOT_WS_URL, is_spot=True))
    asyncio.create_task(listen_trades(FUTURES_WS_URL, is_spot=False))
    asyncio.create_task(listen_book_ticker())
    asyncio.create_task(fetch_oi_loop())
    asyncio.create_task(display_context())

@app.post("/webhook")
async def receive_webhook(alert: TVAlert):
    print(f"\n[!] Webhook recibido: {alert.signal} en {alert.pair}")
    
    # Analyze High-Probability Context
    # Example logic for a "BUY" signal
    is_buy_signal = "COMPRA" in alert.signal.upper() or "BUY" in alert.signal.upper()
    is_sell_signal = "VENTA" in alert.signal.upper() or "SELL" in alert.signal.upper()
    
    # Calculate OI delta
    oi_delta_pct = 0.0
    if ctx.oi_5m_ago > 0:
        oi_delta_pct = ((ctx.oi_current - ctx.oi_5m_ago) / ctx.oi_5m_ago) * 100
    
    # Determine the context verdict
    verdict = ""
    prob_score = 0
    
    if is_buy_signal:
        if ctx.spot_cvd > 0: prob_score += 1     # +Spot buying
        if ctx.futures_cvd > 0: prob_score += 1  # +Futures buying
        if oi_delta_pct > 0: prob_score += 1     # +OI rising (new longs)
        if ctx.best_bid_qty * ctx.best_bid_price > (ctx.best_ask_qty * ctx.best_ask_price) * 1.5: 
            prob_score += 1                      # +Bid protection barrier
            
        if prob_score >= 3:
            verdict = "🔥 ALTA PROBABILIDAD (Confirmado por Order Flow)"
        else:
            verdict = "⚠️ BAJA PROBABILIDAD (Order Flow Dividido)"
            
    elif is_sell_signal:
        if ctx.spot_cvd < 0: prob_score += 1     # +Spot selling
        if ctx.futures_cvd < 0: prob_score += 1  # +Futures selling
        if oi_delta_pct > 0: prob_score += 1     # +OI rising (new shorts)
        if ctx.best_ask_qty * ctx.best_ask_price > (ctx.best_bid_qty * ctx.best_bid_price) * 1.5: 
            prob_score += 1                      # +Ask resistance barrier
            
        if prob_score >= 3:
            verdict = "🔥 ALTA PROBABILIDAD (Confirmado por Order Flow)"
        else:
            verdict = "⚠️ BAJA PROBABILIDAD (Order Flow Dividido)"
    else:
        verdict = "Señal Neutral"
            
    # Always format the complete message so the user can make the final decision
    alert_text = (
        f"🚨 <b>SEÑAL TRADINGVIEW</b> 🚨\n\n"
        f"<b>Par:</b> {alert.pair} ({alert.timeframe})\n"
        f"<b>Señal:</b> {alert.signal}\n"
        f"<b>Precio Actual:</b> ${alert.price:,.2f}\n"
        f"<b>Veredicto Bot:</b> <b>{verdict}</b>\n\n"
        f"📊 <b>CONTEXTO DE MERCADO EN VIVO</b>\n"
        f"├─ <b>CVD Spot:</b> ${ctx.spot_cvd:,.0f}\n"
        f"├─ <b>CVD Futuros:</b> ${ctx.futures_cvd:,.0f}\n"
        f"├─ <b>Delta OI (5m):</b> {oi_delta_pct:+.3f}%\n"
        f"└─ <b>Book BBO (Bids):</b> ${ctx.best_bid_qty * ctx.best_bid_price:,.0f}\n"
        f"└─ <b>Book BBO (Asks):</b> ${ctx.best_ask_qty * ctx.best_ask_price:,.0f}\n"
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
