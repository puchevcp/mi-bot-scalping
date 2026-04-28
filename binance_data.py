import asyncio
import json
import websockets
from datetime import datetime, timezone
import aiohttp
from telegram_notifier import send_telegram_message

# Endpoints
FUTURES_WS_URL = "wss://fstream.binance.com/stream"
SPOT_WS_URL = "wss://stream.binance.com/stream"
SYMBOL = "btcusdt"

# Market Context (Global State)
class MarketContext:
    def __init__(self):
        self.price = 0.0
        self.spot_cvd = 0.0
        self.futures_cvd = 0.0
        self.oi_current = 0.0
        self.oi_5m_ago = 0.0
        self.oi_history = []
        self.bids = {}
        self.asks = {}
        self.last_update_id = 0
        self.depth_0_5_delta_usd = 0.0
        self.heatmap_walls = []
        self.recent_liquidations = []
        self.volume_profile = {}
        self.session_poc_price = 0.0
        self.tracked_walls = {}
        self.current_session_day = datetime.now(timezone.utc).day
        self.last_futures_msg = datetime.now()
        self.last_spot_msg = datetime.now()

ctx = MarketContext()

async def fetch_price_fallback():
    """ Respaldo REST para el precio si los WS fallan. """
    url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL.upper()}"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if ctx.price == 0 or (datetime.now() - ctx.last_futures_msg).total_seconds() > 15:
                    async with session.get(url, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            ctx.price = float(data['price'])
            except: pass
            await asyncio.sleep(5)

async def listen_futures_combined():
    """ 
    CONEXIÓN MAESTRA: Escucha Trades, Depth y Liquidaciones en un solo flujo. 
    Esto evita bloqueos de IP en Render.
    """
    streams = f"{SYMBOL}@aggTrade/{SYMBOL}@depth@100ms/{SYMBOL}@forceOrder"
    url = f"{FUTURES_WS_URL}?streams={streams}"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                print(f"[OK] Conectado al Flujo Maestro de Futuros")
                while True:
                    response = await ws.recv()
                    raw = json.loads(response)
                    stream = raw.get('stream', '')
                    data = raw.get('data', {})
                    ctx.last_futures_msg = datetime.now()
                    
                    # 1. TRADES (CVD & POC)
                    if "@aggTrade" in stream:
                        price = float(data['p'])
                        qty = float(data['q'])
                        is_buyer_maker = data['m']
                        volume_usd = price * qty
                        
                        # Reset Diario
                        now_utc = datetime.now(timezone.utc)
                        if now_utc.day != ctx.current_session_day:
                            ctx.futures_cvd = 0.0
                            ctx.spot_cvd = 0.0
                            ctx.volume_profile.clear()
                            ctx.current_session_day = now_utc.day
                        
                        ctx.price = price
                        if is_buyer_maker: ctx.futures_cvd -= volume_usd
                        else: ctx.futures_cvd += volume_usd
                        
                        # Volume Profile & POC
                        rounded = round(price / 50) * 50
                        ctx.volume_profile[rounded] = ctx.volume_profile.get(rounded, 0) + volume_usd
                        if ctx.volume_profile:
                            ctx.session_poc_price = max(ctx.volume_profile, key=ctx.volume_profile.get)

                    # 2. DEPTH (Orderbook & Heatmap)
                    elif "@depth" in stream:
                        if data.get('u', 0) <= ctx.last_update_id: continue
                        for p_str, q_str in data.get('b', []):
                            p, q = float(p_str), float(q_str)
                            if q == 0.0: ctx.bids.pop(p, None)
                            else: ctx.bids[p] = q
                        for p_str, q_str in data.get('a', []):
                            p, q = float(p_str), float(q_str)
                            if q == 0.0: ctx.asks.pop(p, None)
                            else: ctx.asks[p] = q
                        ctx.last_update_id = data['u']
                        
                        # Recalcular Heatmap cada X mensajes
                        if data['u'] % 10 == 0 and ctx.price > 0:
                            # 0-5% Delta
                            l5_bid, l5_ask = ctx.price * 0.95, ctx.price * 1.05
                            b5 = sum(p*q for p,q in ctx.bids.items() if p >= l5_bid)
                            a5 = sum(p*q for p,q in ctx.asks.items() if p <= l5_ask)
                            ctx.depth_0_5_delta_usd = b5 - a5
                            
                            # Muros (Threshold 400 BTC)
                            walls = []
                            for p, q in ctx.bids.items():
                                if q >= 400 and p >= l5_bid: walls.append((p, q, 'BID (Soporte)'))
                            for p, q in ctx.asks.items():
                                if q >= 400 and p <= l5_ask: walls.append((p, q, 'ASK (Resistencia)'))
                            ctx.heatmap_walls = sorted(walls, key=lambda x: x[1], reverse=True)

                    # 3. LIQUIDATIONS
                    elif "@forceOrder" in stream:
                        order = data.get('o', {})
                        side = "LONG" if order.get('S') == "SELL" else "SHORT"
                        val = float(order.get('p', 0)) * float(order.get('q', 0))
                        ctx.recent_liquidations.append((datetime.now(), side, val))
                        # Limpiar viejas (>15m)
                        cutoff = datetime.now().timestamp() - 900
                        ctx.recent_liquidations = [x for x in ctx.recent_liquidations if x[0].timestamp() > cutoff]

        except Exception as e:
            print(f"[!] Error en Flujo Maestro Futuros: {e}. Reconectando...")
            await asyncio.sleep(2)

async def listen_spot_combined():
    """ Unifica flujo de Spot para mayor estabilidad. """
    url = f"{SPOT_WS_URL}?streams={SYMBOL}@aggTrade"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                print(f"[OK] Conectado al Flujo Maestro Spot")
                while True:
                    response = await ws.recv()
                    raw = json.loads(response)
                    data = raw.get('data', {})
                    ctx.last_spot_msg = datetime.now()
                    
                    if 'p' in data:
                        p, q = float(data['p']), float(data['q'])
                        vol = p * q
                        if data['m']: ctx.spot_cvd -= vol
                        else: ctx.spot_cvd += vol
        except Exception as e:
            await asyncio.sleep(2)

async def fetch_oi_loop():
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL.upper()}"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        val = float(data.get('openInterest', 0))
                        ctx.oi_current = val
                        now = datetime.now()
                        ctx.oi_history.append((now, val))
                        cutoff = now.timestamp() - 300
                        ctx.oi_history = [x for x in ctx.oi_history if x[0].timestamp() > cutoff]
                        if ctx.oi_history: ctx.oi_5m_ago = ctx.oi_history[0][1]
            except: pass
            await asyncio.sleep(5)

async def display_context():
    while True:
        await asyncio.sleep(3)
        now = datetime.now().strftime("%H:%M:%S")
        oi_pct = ((ctx.oi_current - ctx.oi_5m_ago) / ctx.oi_5m_ago * 100) if ctx.oi_5m_ago > 0 else 0
        
        c_spot = "\033[92m" if ctx.spot_cvd > 0 else "\033[91m"
        c_fut = "\033[92m" if ctx.futures_cvd > 0 else "\033[91m"
        reset = "\033[0m"
        
        best_wall = f"{ctx.heatmap_walls[0][1]:.0f} BTC en ${ctx.heatmap_walls[0][0]:,.0f}" if ctx.heatmap_walls else "Ninguno"
        l_sum = sum(v for t,s,v in ctx.recent_liquidations if s=="LONG")
        s_sum = sum(v for t,s,v in ctx.recent_liquidations if s=="SHORT")

        print(f"\n[{now}] PRECIO: ${ctx.price:,.2f} | POC: ${ctx.session_poc_price:,.2f}")
        print(f"|- CVD Spot: {c_spot}${ctx.spot_cvd:,.0f}{reset} | Fut: {c_fut}${ctx.futures_cvd:,.0f}{reset}")
        print(f"|- OI: {ctx.oi_current:,.2f} BTC ({oi_pct:+.3f}%) | Delta 5%: ${ctx.depth_0_5_delta_usd:,.0f}")
        print(f"|- Muro: {best_wall} | Liqs: L:${l_sum:,.0f} S:${s_sum:,.0f}")

async def main():
    # Cargar snapshot del orderbook al inicio
    url = f"https://fapi.binance.com/fapi/v1/depth?symbol={SYMBOL.upper()}&limit=1000"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                ctx.last_update_id = data.get('lastUpdateId', 0)
                ctx.bids = {float(p): float(q) for p, q in data.get('bids', [])}
                ctx.asks = {float(p): float(q) for p, q in data.get('asks', [])}

    await asyncio.gather(
        listen_futures_combined(),
        listen_spot_combined(),
        fetch_oi_loop(),
        fetch_price_fallback(),
        display_context()
    )

if __name__ == "__main__":
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
