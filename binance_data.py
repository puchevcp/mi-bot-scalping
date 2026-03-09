import asyncio
import json
import websockets
from datetime import datetime
import aiohttp
from telegram_notifier import send_telegram_message

# Endpoints
FUTURES_WS_URL = "wss://fstream.binance.com/ws"
SPOT_WS_URL = "wss://stream.binance.com:9443/ws"
SYMBOL = "btcusdt"

# Market Context (Global State)
class MarketContext:
    def __init__(self):
        self.price = 0.0
        
        # Cumulative Volume Delta (CVD)
        self.spot_cvd = 0.0
        self.futures_cvd = 0.0
        
        # Open Interest
        self.oi_current = 0.0
        self.oi_5m_ago = 0.0
        self.oi_history = [] # Para guardar el historico de los ultimos 5 min
        
        # Order Book BBO (Best Bid / Best Offer)
        self.best_bid_price = 0.0
        self.best_bid_qty = 0.0
        self.best_ask_price = 0.0
        self.best_ask_qty = 0.0

ctx = MarketContext()

async def listen_trades(ws_url, is_spot=False):
    """ Escucha agresiones a mercado (AggTrades) para calcular el CVD """
    url = f"{ws_url}/{SYMBOL}@aggTrade"
    name = "SPOT" if is_spot else "FUTURES"
    
    while True:
        try:
            async with websockets.connect(url) as ws:
                print(f"[*] Conectado a AggTrades ({name})")
                while True:
                    response = await ws.recv()
                    data = json.loads(response)
                    
                    price = float(data['p'])
                    qty = float(data['q'])
                    is_buyer_maker = data['m'] # True = Sell a mercado, False = Buy a mercado
                    volume_usd = price * qty
                    
                    if not is_spot:
                        ctx.price = price # Actualizamos precio global con futuros
                    
                    # Logica CVD
                    if is_buyer_maker: # Venta
                        if is_spot: ctx.spot_cvd -= volume_usd
                        else: ctx.futures_cvd -= volume_usd
                    else:              # Compra
                        if is_spot: ctx.spot_cvd += volume_usd
                        else: ctx.futures_cvd += volume_usd
                        
                    # Mantenemos las alertas de super ballenas en futuros
                    if not is_spot and volume_usd >= 1000000:
                        trade_dir = "VENTArojo" if is_buyer_maker else "COMPRAverde"
                        asyncio.create_task(send_telegram_message(
                            f"🐋 <b>SUPER BALLENA FUTUROS</b>\n{trade_dir} de ${volume_usd:,.0f} a ${price:,.2f}"
                        ))
                        
        except Exception as e:
            print(f"[!] Error en trades WS {name}: {e}. Reconectando...")
            await asyncio.sleep(2)

async def listen_book_ticker():
    """ Escucha el BBO (Mejor Bid y Mejor Ask) para ver la liquidez inmediata """
    url = f"{FUTURES_WS_URL}/{SYMBOL}@bookTicker"
    
    while True:
        try:
            async with websockets.connect(url) as ws:
                print(f"[*] Conectado a Book Ticker (Liquidez Inmediata)")
                while True:
                    response = await ws.recv()
                    data = json.loads(response)
                    
                    ctx.best_bid_price = float(data['b'])
                    ctx.best_bid_qty = float(data['B'])
                    ctx.best_ask_price = float(data['a'])
                    ctx.best_ask_qty = float(data['A'])
        except Exception as e:
            await asyncio.sleep(2)

async def fetch_oi_loop():
    """ Consulta el Open Interest via REST periodicamente para ver la variacion """
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL.upper()}"
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        current_oi = float(data.get('openInterest', 0))
                        
                        # Actualizar estado e historial
                        ctx.oi_current = current_oi
                        now = datetime.now()
                        ctx.oi_history.append((now, current_oi))
                        
                        # Limpiar historial viejo (> 5 mins) y obtener el oi_5m_ago
                        cutoff = now.timestamp() - 300
                        ctx.oi_history = [(t, v) for t, v in ctx.oi_history if t.timestamp() > cutoff]
                        if ctx.oi_history:
                            ctx.oi_5m_ago = ctx.oi_history[0][1] # El elemento mas antiguo en la ventana de 5m
            except Exception as e:
                pass
            await asyncio.sleep(5) # Evitar rate limits

async def display_context():
    """ Muestra la matriz de informacion completa en consola """
    print("\n" + "="*50)
    print("HIGH-PROBABILITY MARKET CONTEXT ENGINE")
    print("="*50)
    
    while True:
        await asyncio.sleep(3)
        now = datetime.now().strftime("%H:%M:%S")
        
        # Calcular delta de OI
        oi_delta_pct = 0.0
        if ctx.oi_5m_ago > 0:
            oi_delta_pct = ((ctx.oi_current - ctx.oi_5m_ago) / ctx.oi_5m_ago) * 100
        
        # Formatos de color
        s_cvd_color = "\033[92m" if ctx.spot_cvd > 0 else "\033[91m"
        f_cvd_color = "\033[92m" if ctx.futures_cvd > 0 else "\033[91m"
        oi_color = "\033[92m" if oi_delta_pct > 0 else "\033[91m"
        reset = "\033[0m"
        
        # Estado del Order Book Protegido
        bid_usd = ctx.best_bid_qty * ctx.best_bid_price
        ask_usd = ctx.best_ask_qty * ctx.best_ask_price
        book_status = "Equilibrado"
        if bid_usd > ask_usd * 1.5: book_status = "Fuerte Soporte (Bids>Asks)"
        elif ask_usd > bid_usd * 1.5: book_status = "Fuerte Resistencia (Asks>Bids)"
        
        print(f"\n[{now}] PRECIO BTC: ${ctx.price:,.2f}")
        print(f"├─ CVD Spot   : {s_cvd_color}${ctx.spot_cvd:,.0f}{reset}")
        print(f"├─ CVD Futuros: {f_cvd_color}${ctx.futures_cvd:,.0f}{reset}")
        print(f"├─ Open I.(5m): {oi_color}{ctx.oi_current:,.2f} BTC ({oi_delta_pct:+.3f}%){reset}")
        print(f"└─ Book (BBO) : {book_status} | Bids: ${bid_usd:,.0f} vs Asks: ${ask_usd:,.0f}")

async def main():
    await asyncio.gather(
        listen_trades(SPOT_WS_URL, is_spot=True),
        listen_trades(FUTURES_WS_URL, is_spot=False),
        listen_book_ticker(),
        fetch_oi_loop(),
        display_context()
    )

if __name__ == "__main__":
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMotor detenido.")
