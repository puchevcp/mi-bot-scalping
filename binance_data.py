import asyncio
import json
import websockets
from datetime import datetime, timezone
import aiohttp
from telegram_notifier import send_telegram_message

# Endpoints
FUTURES_WS_URL = "wss://fstream.binance.com/ws"
SPOT_WS_URL    = "wss://stream.binance.com/ws"
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
        self.oi_history = []
        
        # Heatmap / Depth (Local Order Book Cache)
        self.bids = {}
        self.asks = {}
        self.last_update_id = 0
        
        self.depth_0_5_delta_usd = 0.0
        self.heatmap_walls = []
        
        # Liquidations (Rekt Stream)
        self.recent_liquidations = []
        
        # Volume Profile (POC)
        self.volume_profile = {}
        self.session_poc_price = 0.0
        
        # V2: Dynamic Tracking of Heatmap Limit Walls
        self.tracked_walls = {}
        
        # Tracking the current UTC Day to reset "Session" metrics
        self.current_session_day = datetime.now(timezone.utc).day
        
        # Watchdog timestamps
        self.last_futures_msg = datetime.now()
        self.last_spot_msg    = datetime.now()

ctx = MarketContext()

# ---------------------------------------------------------------------------
# RESPALDO REST: precio si el WS de futuros falla por mas de 15s
# ---------------------------------------------------------------------------
async def fetch_price_fallback():
    url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={SYMBOL.upper()}"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                stale = (datetime.now() - ctx.last_futures_msg).total_seconds() > 15
                if ctx.price == 0 or stale:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            ctx.price = float(data['price'])
                            if stale:
                                print(f"[REST] Precio obtenido por fallback: ${ctx.price:,.2f}")
            except Exception:
                pass
            await asyncio.sleep(5)

# ---------------------------------------------------------------------------
# CVD FUTUROS VIA REST (fallback cuando fstream.binance.com está bloqueado)
# Usa /fapi/v1/aggTrades para calcular el delta de cada ciclo de polling.
# ---------------------------------------------------------------------------
async def fetch_futures_cvd_rest():
    """
    Calcula el CVD de Futuros usando la API REST en lugar del WebSocket.
    Esto es necesario cuando fstream.binance.com está bloqueado desde Render.
    """
    url = f"https://fapi.binance.com/fapi/v1/aggTrades?symbol={SYMBOL.upper()}&limit=100"
    last_trade_id = None
    
    async with aiohttp.ClientSession() as session:
        print(f"[REST] Iniciando CVD Futuros via REST (fallback)")
        while True:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        trades = await resp.json()
                        if not trades:
                            await asyncio.sleep(2)
                            continue
                        
                        # Solo procesar trades nuevos (más recientes que el último ID visto)
                        new_trades = trades
                        if last_trade_id is not None:
                            new_trades = [t for t in trades if t['a'] > last_trade_id]
                        
                        if new_trades:
                            last_trade_id = new_trades[-1]['a']
                            ctx.last_futures_msg = datetime.now()
                            
                            for trade in new_trades:
                                price  = float(trade['p'])
                                qty    = float(trade['q'])
                                vol    = price * qty
                                is_seller = trade['m']  # True = venta a mercado
                                
                                # Actualizar precio
                                ctx.price = price
                                
                                # CVD
                                if is_seller: ctx.futures_cvd -= vol
                                else:         ctx.futures_cvd += vol
                                
                                # Volume Profile & POC
                                rounded = round(price / 50) * 50
                                ctx.volume_profile[rounded] = ctx.volume_profile.get(rounded, 0) + vol
                            
                            if ctx.volume_profile:
                                ctx.session_poc_price = max(ctx.volume_profile, key=ctx.volume_profile.get)
                        
                        # Reset diario
                        now_utc = datetime.now(timezone.utc)
                        if now_utc.day != ctx.current_session_day:
                            ctx.futures_cvd = 0.0
                            ctx.spot_cvd    = 0.0
                            ctx.volume_profile.clear()
                            ctx.session_poc_price   = 0.0
                            ctx.current_session_day = now_utc.day
                            last_trade_id           = None
                            print(f"[RESET] Sesion UTC reiniciada.")

            except Exception as e:
                print(f"[REST CVD] Error: {e}")
            
            await asyncio.sleep(2)  # Polling cada 2 segundos

# ---------------------------------------------------------------------------
# CVD: AggTrades (Spot y Futuros por separado — igual que version 3m)
# ---------------------------------------------------------------------------
async def listen_trades(ws_url, is_spot=False):
    """ Escucha agresiones a mercado (AggTrades) para calcular el CVD """
    url  = f"{ws_url}/{SYMBOL}@aggTrade"
    name = "SPOT" if is_spot else "FUTURES"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                print(f"[OK] Conectado a AggTrades ({name})")
                while True:
                    response = await ws.recv()
                    data = json.loads(response)
                    
                    # Soporte dual: directo o envuelto en 'data'
                    if 'data' in data:
                        data = data['data']
                    
                    if 'p' not in data:
                        continue
                    
                    price            = float(data['p'])
                    qty              = float(data['q'])
                    is_buyer_maker   = data['m']   # True = Venta a mercado
                    volume_usd       = price * qty
                    
                    # Reset UTC Medianoche
                    now_utc = datetime.now(timezone.utc)
                    if now_utc.day != ctx.current_session_day:
                        ctx.spot_cvd = 0.0
                        ctx.futures_cvd = 0.0
                        ctx.volume_profile.clear()
                        ctx.session_poc_price = 0.0
                        ctx.current_session_day = now_utc.day
                        print(f"[RESET] Sesion UTC reiniciada: CVD, POC y Perfil de Volumen limpios.")
                    
                    if not is_spot:
                        ctx.price = price
                        ctx.last_futures_msg = datetime.now()
                    else:
                        ctx.last_spot_msg = datetime.now()
                    
                    # Logica CVD
                    if is_buyer_maker:
                        if is_spot: ctx.spot_cvd    -= volume_usd
                        else:       ctx.futures_cvd  -= volume_usd
                    else:
                        if is_spot: ctx.spot_cvd    += volume_usd
                        else:       ctx.futures_cvd  += volume_usd
                    
                    # Volume Profile & POC (solo futuros)
                    if not is_spot:
                        rounded = round(price / 50) * 50
                        ctx.volume_profile[rounded] = ctx.volume_profile.get(rounded, 0) + volume_usd
                        if ctx.volume_profile:
                            ctx.session_poc_price = max(ctx.volume_profile, key=ctx.volume_profile.get)
                    
                    # Alerta de super-ballena (>$4M, deshabilitada por defecto)
                    if not is_spot and volume_usd >= 4_000_000:
                        pass

        except Exception as e:
            print(f"[!] Error en AggTrades WS {name}: {e}. Reconectando...")
            await asyncio.sleep(2)

# ---------------------------------------------------------------------------
# ORDER BOOK LOCAL (Heatmap)
# ---------------------------------------------------------------------------
async def listen_local_orderbook():
    """ Construye y mantiene un Cache Local del Order Book (Heatmap) """
    
    # 1. Snapshot REST inicial
    snapshot_url = f"https://fapi.binance.com/fapi/v1/depth?symbol={SYMBOL.upper()}&limit=1000"
    async with aiohttp.ClientSession() as session:
        async with session.get(snapshot_url) as resp:
            data = await resp.json()
            ctx.last_update_id = data.get('lastUpdateId', 0)
            ctx.bids = {float(p): float(q) for p, q in data.get('bids', [])}
            ctx.asks = {float(p): float(q) for p, q in data.get('asks', [])}
    
    # 2. WebSocket de diferencias
    url = f"{FUTURES_WS_URL}/{SYMBOL}@depth@100ms"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                print(f"[*] Conectado a Order Book Diff Stream (Heatmap Cache)")
                while True:
                    response = await ws.recv()
                    raw  = json.loads(response)
                    data = raw.get('data', raw)
                    
                    if not isinstance(data, dict) or 'u' not in data:
                        continue
                    
                    if data['u'] <= ctx.last_update_id:
                        continue
                    
                    for p_str, q_str in data.get('b', []):
                        p, q = float(p_str), float(q_str)
                        if q == 0.0: ctx.bids.pop(p, None)
                        else:        ctx.bids[p] = q
                    
                    for p_str, q_str in data.get('a', []):
                        p, q = float(p_str), float(q_str)
                        if q == 0.0: ctx.asks.pop(p, None)
                        else:        ctx.asks[p] = q
                    
                    ctx.last_update_id = data['u']
                    
                    # Recalcular Heatmap cada ~10 mensajes
                    if (data['u'] % 10) == 0 and ctx.price > 0:
                        min_p, max_p = ctx.price * 0.90, ctx.price * 1.10
                        ctx.bids = {p: q for p, q in ctx.bids.items() if p > min_p}
                        ctx.asks = {p: q for p, q in ctx.asks.items() if p < max_p}
                        
                        l5_bid = ctx.price * 0.95
                        l5_ask = ctx.price * 1.05
                        
                        b5 = sum(p * q for p, q in ctx.bids.items() if p >= l5_bid)
                        a5 = sum(p * q for p, q in ctx.asks.items() if p <= l5_ask)
                        ctx.depth_0_5_delta_usd = b5 - a5
                        
                        # Muros >= 400 BTC
                        walls = []
                        for p, q in ctx.bids.items():
                            if q >= 400 and p >= l5_bid:
                                walls.append((p, q, 'BID (Soporte)'))
                        for p, q in ctx.asks.items():
                            if q >= 400 and p <= l5_ask:
                                walls.append((p, q, 'ASK (Resistencia)'))
                        
                        ctx.heatmap_walls = sorted(walls, key=lambda x: x[1], reverse=True)
                        
                        # V2: Rastreo dinamico de muros
                        current_wall_prices = set()
                        for p, q, w_type in ctx.heatmap_walls:
                            current_wall_prices.add(p)
                            if p not in ctx.tracked_walls:
                                ctx.tracked_walls[p] = (q, w_type)
                                try:
                                    asyncio.create_task(send_telegram_message(
                                        f"🚨 <b>¡NUEVO MURO DETECTADO!</b>\n{q:,.0f} BTC Limit en ${p:,.2f} ({w_type})"
                                    ))
                                except Exception:
                                    pass
                            else:
                                ctx.tracked_walls[p] = (q, w_type)
                        
                        for p in list(ctx.tracked_walls.keys()):
                            if p not in current_wall_prices:
                                old_q, w_type = ctx.tracked_walls[p]
                                actual_q = ctx.bids.get(p, 0) if "BID" in w_type else ctx.asks.get(p, 0)
                                if actual_q < 100:
                                    distancia = abs(ctx.price - p) / ctx.price
                                    if distancia <= 0.06:
                                        try:
                                            asyncio.create_task(send_telegram_message(
                                                f"👻 <b>¡MURO ELIMINADO/CONSUMIDO!</b>\nEl muro de ${p:,.2f} ({w_type}) ha desaparecido. (Restante: {actual_q:,.0f} BTC)"
                                            ))
                                        except Exception:
                                            pass
                                    del ctx.tracked_walls[p]

        except Exception as e:
            print(f"[!] Error en Order Book WS: {e}. Reconectando...")
            await asyncio.sleep(2)

# ---------------------------------------------------------------------------
# LIQUIDACIONES (Rekt Stream)
# ---------------------------------------------------------------------------
async def listen_liquidations():
    """ Escucha liquidaciones en tiempo real """
    url = f"{FUTURES_WS_URL}/{SYMBOL}@forceOrder"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                print(f"[*] Conectado a Liquidaciones (Rekt Stream)")
                while True:
                    response = await ws.recv()
                    raw  = json.loads(response)
                    data = raw.get('data', raw)
                    
                    order_data = data.get('o', {})
                    if not order_data:
                        continue
                    
                    side       = order_data.get('S')
                    liq_type   = "LONG" if side == "SELL" else "SHORT"
                    price      = float(order_data.get('p', 0))
                    qty        = float(order_data.get('q', 0))
                    volume_usd = price * qty
                    
                    now = datetime.now()
                    ctx.recent_liquidations.append((now, liq_type, volume_usd))
                    
                    cutoff = now.timestamp() - 900
                    ctx.recent_liquidations = [
                        (t, l, v) for t, l, v in ctx.recent_liquidations if t.timestamp() > cutoff
                    ]
        except Exception as e:
            print(f"[!] Error en Liquidaciones WS: {e}. Reconectando...")
            await asyncio.sleep(2)

# ---------------------------------------------------------------------------
# OPEN INTEREST (REST polling)
# ---------------------------------------------------------------------------
async def fetch_oi_loop():
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={SYMBOL.upper()}"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data  = await resp.json()
                        val   = float(data.get('openInterest', 0))
                        now   = datetime.now()
                        ctx.oi_current = val
                        ctx.oi_history.append((now, val))
                        cutoff = now.timestamp() - 300
                        ctx.oi_history = [(t, v) for t, v in ctx.oi_history if t.timestamp() > cutoff]
                        if ctx.oi_history:
                            ctx.oi_5m_ago = ctx.oi_history[0][1]
            except Exception:
                pass
            await asyncio.sleep(5)

# ---------------------------------------------------------------------------
# WATCHDOG
# ---------------------------------------------------------------------------
async def ws_watchdog():
    """ Alerta si los WS llevan mas de 30s sin datos. """
    while True:
        await asyncio.sleep(30)
        now = datetime.now()
        fut_stale = (now - ctx.last_futures_msg).total_seconds()
        spt_stale = (now - ctx.last_spot_msg).total_seconds()
        if fut_stale > 30:
            print(f"[⚠️ WATCHDOG] Futuros sin datos hace {fut_stale:.0f}s")
        if spt_stale > 30:
            print(f"[⚠️ WATCHDOG] Spot sin datos hace {spt_stale:.0f}s")

# ---------------------------------------------------------------------------
# DISPLAY (consola — igual que version 3m)
# ---------------------------------------------------------------------------
async def display_context():
    print("\n" + "="*50)
    print("HIGH-PROBABILITY MARKET CONTEXT ENGINE")
    print("="*50)
    
    while True:
        await asyncio.sleep(3)
        now = datetime.now().strftime("%H:%M:%S")
        
        oi_delta_pct = 0.0
        if ctx.oi_5m_ago > 0:
            oi_delta_pct = ((ctx.oi_current - ctx.oi_5m_ago) / ctx.oi_5m_ago) * 100
        
        s_cvd_color   = "\033[92m" if ctx.spot_cvd    > 0 else "\033[91m"
        f_cvd_color   = "\033[92m" if ctx.futures_cvd > 0 else "\033[91m"
        oi_color      = "\033[92m" if oi_delta_pct    > 0 else "\033[91m"
        delta_color   = "\033[92m" if ctx.depth_0_5_delta_usd > 0 else "\033[91m"
        reset         = "\033[0m"
        
        wall_str = "Ninguno"
        if ctx.heatmap_walls:
            w = ctx.heatmap_walls[0]
            wall_str = f"{w[1]:.0f} BTC en ${w[0]:,.0f} ({w[2]})"
        
        long_liqs  = sum(v for t, l, v in ctx.recent_liquidations if l == "LONG")
        short_liqs = sum(v for t, l, v in ctx.recent_liquidations if l == "SHORT")
        
        poc_status = "Neutral"
        if ctx.price > ctx.session_poc_price > 0:
            poc_status = "Sobre el POC (Alcista)"
        elif ctx.price < ctx.session_poc_price > 0:
            poc_status = "Bajo el POC (Bajista)"
        
        print(f"\n[{now}] PRECIO BTC: ${ctx.price:,.2f} | POC Sesion: ${ctx.session_poc_price:,.2f} ({poc_status})")
        print(f"|- CVD Spot   : {s_cvd_color}${ctx.spot_cvd:,.0f}{reset}")
        print(f"|- CVD Futuros: {f_cvd_color}${ctx.futures_cvd:,.0f}{reset}")
        print(f"|- Open I.(5m): {oi_color}{ctx.oi_current:,.2f} BTC ({oi_delta_pct:+.3f}%){reset}")
        print(f"|- Delta 0-5% : {delta_color}${ctx.depth_0_5_delta_usd:,.0f}{reset}")
        print(f"|- [W] MURO 500+: {wall_str}")
        print(f"`- Liqs (15m) : Longs liquidados: ${long_liqs:,.0f} | Shorts liquidados: ${short_liqs:,.0f}")

# ---------------------------------------------------------------------------
# MAIN (solo para correr standalone en desarrollo)
# ---------------------------------------------------------------------------
async def main():
    await asyncio.gather(
        listen_trades(SPOT_WS_URL, is_spot=True),
        listen_trades(FUTURES_WS_URL, is_spot=False),
        listen_local_orderbook(),
        listen_liquidations(),
        fetch_oi_loop(),
        fetch_price_fallback(),
        ws_watchdog(),
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
