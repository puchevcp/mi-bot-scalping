from fastapi import FastAPI, Request
import uvicorn
from pydantic import BaseModel
from telegram_notifier import send_telegram_message
import asyncio

app = FastAPI(title="TradingView Webhook Listener")

class TVAlert(BaseModel):
    # This is exactly what we will tell TradingView to send in the alert message
    pair: str         # e.g., "BTCUSDT"
    timeframe: str    # e.g., "15m"
    signal: str       # e.g., "COMPRA (Market Cipher Verde)"
    price: float      # e.g., 67500.50
    optional_msg: str = "" # Any extra note

@app.post("/webhook")
async def receive_webhook(alert: TVAlert):
    print(f"[*] Webhook recibido de TradingView para {alert.pair}")
    
    # Format the message for Telegram
    alert_text = (
        f"🚨 <b>ALERTA TRADINGVIEW</b> 🚨\n\n"
        f"<b>Par:</b> {alert.pair}\n"
        f"<b>Temporalidad:</b> {alert.timeframe}\n"
        f"<b>Señal:</b> {alert.signal}\n"
        f"<b>Precio Actual:</b> ${alert.price:,.2f}\n"
    )
    
    if alert.optional_msg:
        alert_text += f"\n<i>Nota: {alert.optional_msg}</i>"
        
    # Send it using our existing Telegram notifier
    # We don't await the result directly to respond to TradingView as fast as possible (200 OK)
    asyncio.create_task(send_telegram_message(alert_text))
    
    return {"status": "success", "message": "Alert received and sent to Telegram"}

@app.get("/")
def read_root():
    return {"status": "online", "service": "TradingView Webhook Listener"}

if __name__ == "__main__":
    print("[*] Iniciando servidor de Webhooks en http://localhost:8000")
    print("[*] Endpoint para TradingView: http://localhost:8000/webhook")
    uvicorn.run(app, host="0.0.0.0", port=8000)
