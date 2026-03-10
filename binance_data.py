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
        
        # Heatmap / Depth (Local Order Book Cache)
        self.bids = {} # {price: qty}
        self.asks = {}
        self.last_update_id = 0
        
        self.depth_0_5_delta_usd = 0.0 # Bids(0-5%) - Asks(0-5%)
        self.heatmap_walls = [] # [(price, btc_qty, 'BID'/'ASK')]
        
        # Liquidations (Rekt Stream)
        self.recent_liquidations = [] # [(timestamp, 'LONG'/'SHORT', usd_value)]
        
        # Volume Profile (POC)
        self.volume_profile = {} # {rounded_price: volume_usd}
        self.session_poc_price = 0.0

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
                        
                    # Volume Profile (Solo usamos futuros para el POC)
                    if not is_spot:
                        rounded_price = round(price / 50) * 50 # Agrupamos perfil de volumen cada $50
                        ctx.volume_profile[rounded_price] = ctx.volume_profile.get(rounded_price, 0) + volume_usd
                        # Actualizar POC (Point of Control)
                        if ctx.volume_profile:
                            ctx.session_poc_price = max(ctx.volume_profile, key=ctx.volume_profile.get)
                        
                    # Mantenemos las alertas de super ballenas en futuros
                    if not is_spot and volume_usd >= 1000000:
                        trade_dir = "VENTA 🔴" if is_buyer_maker else "COMPRA 🟢"
                        asyncio.create_task(send_telegram_message(
                            f"🐋 <b>SUPER BALLENA FUTUROS</b>\n{trade_dir} de ${volume_usd:,.0f} a ${price:,.2f}"
                        ))
                        
        except Exception as e:
            print(f"[!] Error en trades WS {name}: {e}. Reconectando...")
            await asyncio.sleep(2)

async def listen_local_orderbook():
    """ Construye y mantiene un Cache Local del Order Book para Kiyotaka Heatmap """
    
    # 1. Obtener REST Snapshot (1000 niveles)
    snapshot_url = f"https://fapi.binance.com/fapi/v1/depth?symbol={SYMBOL.upper()}&limit=1000"
    async with aiohttp.ClientSession() as session:
        async with session.get(snapshot_url) as resp:
            data = await resp.json()
            ctx.last_update_id = data.get('lastUpdateId', 0)
            
            ctx.bids = {float(p): float(q) for p, q in data.get('bids', [])}
            ctx.asks = {float(p): float(q) for p, q in data.get('asks', [])}
            
    # 2. Conectar al WebSocket de Updates y mantener cache
    url = f"{FUTURES_WS_URL}/{SYMBOL}@depth@100ms"
    
    while True:
        try:
            async with websockets.connect(url) as ws:
                print(f"[*] Conectado a Order Book Diff Stream (Heatmap Cache)")
                while True:
                    response = await ws.recv()
                    data = json.loads(response)
                    
                    if data['u'] <= ctx.last_update_id:
                        continue # Descartar updates viejos
                        
                    # Aplicar Diff (Si qty es 0.00, se borra el nivel)
                    for p_str, q_str in data.get('b', []):
                        p, q = float(p_str), float(q_str)
                        if q == 0.0: ctx.bids.pop(p, None)
                        else: ctx.bids[p] = q
                        
                    for p_str, q_str in data.get('a', []):
                        p, q = float(p_str), float(q_str)
                        if q == 0.0: ctx.asks.pop(p, None)
                        else: ctx.asks[p] = q
                        
                    ctx.last_update_id = data['u']
                    
                    # --- Calcular Heatmap Data una vez por segundo (aprox cada 10 mensajes) ---
                    if (data['u'] % 5) == 0 and ctx.price > 0:
                        # Limpiar precios muy lejanos para no saturar RAM
                        min_p, max_p = ctx.price * 0.90, ctx.price * 1.10
                        ctx.bids = {p: q for p, q in ctx.bids.items() if p > min_p}
                        ctx.asks = {p: q for p, q in ctx.asks.items() if p < max_p}
                        
                        # 0-5% Depth Delta
                        limit_bid_5 = ctx.price * 0.95
                        limit_ask_5 = ctx.price * 1.05
                        
                        bids_5_sum = sum(p * q for p, q in ctx.bids.items() if p >= limit_bid_5)
                        asks_5_sum = sum(p * q for p, q in ctx.asks.items() if p <= limit_ask_5)
                        ctx.depth_0_5_delta_usd = bids_5_sum - asks_5_sum
                        
                        # Buscar Muros (Whales en Límite) > 500 BTC
                        walls = []
                        for p, q in ctx.bids.items():
                            if q >= 500 and p >= limit_bid_5:
                                walls.append((p, q, 'BID (Soporte)'))
                        for p, q in ctx.asks.items():
                            if q >= 500 and p <= limit_ask_5:
                                walls.append((p, q, 'ASK (Resistencia)'))
                                
                        ctx.heatmap_walls = sorted(walls, key=lambda x: x[1], reverse=True) # Sort by amount

        except Exception as e:
            await asyncio.sleep(2)

async def listen_liquidations():
    """ Escucha liquidaciones en tiempo real (Rekt Stream) """
    url = f"{FUTURES_WS_URL}/{SYMBOL}@forceOrder"
    
    while True:
        try:
            async with websockets.connect(url) as ws:
                print(f"[*] Conectado a Liquidaciones (Rekt Stream)")
                while True:
                    response = await ws.recv()
                    data = json.loads(response)
                    
                    order_data = data.get('o', {})
                    if not order_data: continue
                    
                    side = order_data.get('S') # Si es SELL, fue un Long liquidado. Si es BUY, fue un Short liquidado.
                    liq_type = "LONG" if side == "SELL" else "SHORT"
                    
                    price = float(order_data.get('p', 0))
                    qty = float(order_data.get('q', 0))
                    volume_usd = price * qty
                    
                    now = datetime.now()
                    ctx.recent_liquidations.append((now, liq_type, volume_usd))
                    
                    # Limpiar historial viejo de liquidaciones (> 15 mins)
                    cutoff = now.timestamp() - 900
                    ctx.recent_liquidations = [(t, l, v) for t, l, v in ctx.recent_liquidations if t.timestamp() > cutoff]
                    
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
        
        # Order Book (Heatmap)
        delta_color = "\033[92m" if ctx.depth_0_5_delta_usd > 0 else "\033[91m"
        
        wall_str = "Ninguno"
        if ctx.heatmap_walls:
            best_wall = ctx.heatmap_walls[0]
            wall_str = f"{best_wall[1]:.0f} BTC en ${best_wall[0]:,.0f} ({best_wall[2]})"
        
        # Liquidaciones Recientes
        long_liqs = sum(v for t, l, v in ctx.recent_liquidations if l == "LONG")
        short_liqs = sum(v for t, l, v in ctx.recent_liquidations if l == "SHORT")
        
        # Relacion al POC
        poc_status = "Neutral"
        if ctx.price > ctx.session_poc_price > 0: poc_status = "Sobre el POC (Alcista)"
        elif ctx.price < ctx.session_poc_price > 0: poc_status = "Bajo el POC (Bajista)"
        
        print(f"\n[{now}] PRECIO BTC: ${ctx.price:,.2f} | POC Sesion: ${ctx.session_poc_price:,.2f} ({poc_status})")
        print(f"├─ CVD Spot   : {s_cvd_color}${ctx.spot_cvd:,.0f}{reset}")
        print(f"├─ CVD Futuros: {f_cvd_color}${ctx.futures_cvd:,.0f}{reset}")
        print(f"├─ Open I.(5m): {oi_color}{ctx.oi_current:,.2f} BTC ({oi_delta_pct:+.3f}%){reset}")
        print(f"├─ Delta 0-5% : {delta_color}${ctx.depth_0_5_delta_usd:,.0f}{reset}")
        print(f"├─ 🐋 MURO 500+: {wall_str}")
        print(f"└─ Liqs (15m) : Longs liquidados: ${long_liqs:,.0f} | Shorts liquidados: ${short_liqs:,.0f}")

async def main():
    await asyncio.gather(
        listen_trades(SPOT_WS_URL, is_spot=True),
        listen_trades(FUTURES_WS_URL, is_spot=False),
        listen_local_orderbook(),
        listen_liquidations(),
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
