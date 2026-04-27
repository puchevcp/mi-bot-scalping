"""
binance_executor.py - Platinum V2 Bot Executor
=================================================
Módulo de ejecución de órdenes en Binance Futures.

Arquitectura DCA:
  - Tramo 1 (0.5% riesgo): Orden MARKET (garantiza entrada en señal explosiva)
  - Tramo 2 (0.5% riesgo): Orden LIMIT   (promedia precio, comisión Maker 0.04%)

Gestión de Riesgo:
  - Max 1 posición activa por par
  - SL fijo 0.25% del precio de entrada promedio
  - TP dinámico desde ATR del webhook (o fallback configurable)
  - Filtro de horario configurable (ej: 9-20hs UTC-3)

Modos:
  - DRY_RUN=true   -> Solo loguea, no envía órdenes
  - USE_TESTNET=true -> Testnet de Binance Futuros (saldo ficticio)
  - USE_TESTNET=false -> Cuenta REAL (¡precaución!)
"""

import os
import math
import json
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

try:
    from binance import Client, ThreadedWebsocketManager
    from binance.exceptions import BinanceAPIException
    BINANCE_OK = True
except ImportError:
    BINANCE_OK = False

load_dotenv()

# ============================================================
# CONFIGURACIÓN
# ============================================================
API_KEY         = os.getenv("BINANCE_API_KEY", "")
API_SECRET      = os.getenv("BINANCE_SECRET_KEY", "")
USE_TESTNET     = os.getenv("USE_TESTNET", "true").lower() == "true"
DRY_RUN         = os.getenv("DRY_RUN", "true").lower() == "true"
RISK_PER_TRANCHE = float(os.getenv("RISK_PER_TRANCHE", "0.005"))  # 0.5%
MAX_TRANCHES    = int(os.getenv("MAX_TRANCHES", "2"))
SL_PCT          = float(os.getenv("STOP_LOSS_PCT", "0.0025"))     # 0.25%
TP_FALLBACK_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.0075"))   # 0.75%
HOUR_START      = int(os.getenv("TRADING_HOUR_START", "9"))
HOUR_END        = int(os.getenv("TRADING_HOUR_END", "20"))
UTC_OFFSET      = -3  # UTC-3

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("BinanceExecutor")

# ============================================================
# ESTADO EN MEMORIA (activo mientras el servidor corre)
# ============================================================
# Estructura: { "BTCUSDT": { "side": "BUY", "entries": [...], "sl_order_id": ..., "tp_order_id": ..., "tranche": 1 } }
active_positions = {}


def _get_client():
    """Crea el cliente de Binance con la configuración correcta."""
    if not BINANCE_OK:
        log.error("python-binance no instalado. Instala con: pip install python-binance")
        return None
    if DRY_RUN:
        log.info("[DRY_RUN] Cliente Binance simulado (sin conexión real)")
        return None

    client = Client(API_KEY, API_SECRET, testnet=USE_TESTNET)
    mode_str = "TESTNET" if USE_TESTNET else "PRODUCCION REAL"
    log.info(f"Conectado a Binance Futures ({mode_str})")
    return client


def _is_trading_hours():
    """Verifica si estamos dentro del horario configurado (UTC-3)."""
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc + timedelta(hours=UTC_OFFSET)
    return HOUR_START <= now_local.hour < HOUR_END


def _get_balance(client):
    """Obtiene el balance disponible de USDT en Futuros."""
    if DRY_RUN:
        return 1000.0  # Balance ficticio en modo DRY_RUN
    try:
        account = client.futures_account_balance()
        for asset in account:
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
    except BinanceAPIException as e:
        log.error(f"Error obteniendo balance: {e}")
    return 0.0


def _get_symbol_info(client, symbol):
    """Obtiene info del símbolo (tick size, step size, precio actual)."""
    if DRY_RUN:
        return {"tick_size": 0.01, "step_size": 0.001, "current_price": None}
    try:
        info = client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == symbol:
                tick_size = None
                step_size = None
                for f in s["filters"]:
                    if f["filterType"] == "PRICE_FILTER":
                        tick_size = float(f["tickSize"])
                    if f["filterType"] == "LOT_SIZE":
                        step_size = float(f["stepSize"])
                price = float(client.futures_symbol_ticker(symbol=symbol)["price"])
                return {"tick_size": tick_size, "step_size": step_size, "current_price": price}
    except Exception as e:
        log.error(f"Error obteniendo info de {symbol}: {e}")
    return None


def _round_price(price, tick_size):
    decimals = max(0, -int(math.floor(math.log10(tick_size))))
    return round(price, decimals)


def _round_qty(qty, step_size):
    decimals = max(0, -int(math.floor(math.log10(step_size))))
    return round(qty, decimals)


def _calc_quantity(balance, tranche_risk, price, step_size, sl_pct):
    """Calcula la cantidad de contratos para arriesgar X% del balance."""
    risk_amount = balance * tranche_risk
    # Posición = risk_amount / SL en USDT del contrato
    sl_amount_per_unit = price * sl_pct
    raw_qty = risk_amount / sl_amount_per_unit
    return _round_qty(raw_qty, step_size)


def _place_sl_tp(client, symbol, side, quantity, entry_price, sl_pct, tp_pct, tick_size):
    """Coloca las órdenes de Stop Loss y Take Profit en la posición."""
    if side == "BUY":
        sl_price = _round_price(entry_price * (1 - sl_pct), tick_size)
        tp_price = _round_price(entry_price * (1 + tp_pct), tick_size)
        close_side = "SELL"
    else:
        sl_price = _round_price(entry_price * (1 + sl_pct), tick_size)
        tp_price = _round_price(entry_price * (1 - tp_pct), tick_size)
        close_side = "BUY"

    if DRY_RUN:
        log.info(f"[DRY_RUN] SL @ {sl_price} | TP @ {tp_price} (Cantidad: {quantity})")
        return {"sl_id": "dry_sl", "tp_id": "dry_tp", "sl_price": sl_price, "tp_price": tp_price}

    sl_order = client.futures_create_order(
        symbol=symbol,
        side=close_side,
        type="STOP_MARKET",
        stopPrice=sl_price,
        closePosition=True
    )
    tp_order = client.futures_create_order(
        symbol=symbol,
        side=close_side,
        type="TAKE_PROFIT_MARKET",
        stopPrice=tp_price,
        closePosition=True
    )
    log.info(f"SL colocado @ {sl_price} | TP colocado @ {tp_price}")
    return {"sl_id": sl_order["orderId"], "tp_id": tp_order["orderId"], "sl_price": sl_price, "tp_price": tp_price}


def _cancel_open_sl_tp(client, symbol, position):
    """Cancela SL y TP previos antes de actualizar la posición."""
    if DRY_RUN:
        return
    for key in ["sl_id", "tp_id"]:
        oid = position.get(key)
        if oid and oid != "dry_sl" and oid != "dry_tp":
            try:
                client.futures_cancel_order(symbol=symbol, orderId=oid)
                log.info(f"Orden {oid} cancelada para recolocar en posicion promediada")
            except BinanceAPIException as e:
                log.warning(f"No se pudo cancelar orden {oid}: {e}")


# ============================================================
# FUNCIÓN PRINCIPAL: PROCESAR SEÑAL DEL WEBHOOK
# ============================================================
def process_signal(signal_data: dict) -> dict:
    """
    Recibe los datos del webhook y ejecuta la lógica DCA en Binance.

    signal_data esperado:
    {
        "symbol":    "BTCUSDT",
        "side":      "BUY" o "SELL",
        "verdict":   "ALTA PROBABILIDAD" / "MEDIA PROBABILIDAD" / ...
        "tp_pct":    0.0075,    # Take Profit % (desde ATR del webhook)
        "sl_pct":    0.0025,    # Stop Loss %
        "score":     8          # Puntaje del indicador
    }

    Returns dict con el resultado de la ejecución.
    """
    symbol = signal_data.get("symbol", "BTCUSDT").upper()
    side   = signal_data.get("side", "BUY").upper()  # "BUY" o "SELL"
    verdict = signal_data.get("verdict", "")
    tp_pct  = float(signal_data.get("tp_pct", TP_FALLBACK_PCT))
    sl_pct  = float(signal_data.get("sl_pct", SL_PCT))

    log.info(f"--- Señal recibida: {side} {symbol} | {verdict} ---")

    # 1. Filtro de horario
    if not _is_trading_hours():
        log.info(f"Fuera de horario de trading ({HOUR_START}-{HOUR_END}hs UTC{UTC_OFFSET}). Señal ignorada.")
        return {"status": "ignored", "reason": "out_of_hours"}

    # 2. Filtro de calidad (solo Alta y Media probabilidad)
    if "BAJA" in verdict.upper():
        log.info(f"Señal de BAJA probabilidad ignorada.")
        return {"status": "ignored", "reason": "low_probability"}

    client = _get_client()

    # 3. Verificar si ya tenemos posición en ese par
    if symbol in active_positions:
        pos = active_positions[symbol]
        
        # Si la nueva señal es en la MISMA dirección: posible tramo 2
        if pos["side"] == side:
            if pos["tranche"] >= MAX_TRANCHES:
                log.info(f"Ya tenemos {MAX_TRANCHES} tramos en {symbol}. Señal ignorada.")
                return {"status": "ignored", "reason": "max_tranches_reached"}
            
            # ---- TRAMO 2: Orden LIMIT para promediar ----
            log.info(f"Señal DCA detectada para {symbol}. Ejecutando TRAMO 2 (LIMIT)...")
            
            info = _get_symbol_info(client, symbol)
            if info is None:
                return {"status": "error", "reason": "no_symbol_info"}
            
            balance = _get_balance(client)
            current_price = info["current_price"] or pos["entries"][0]
            
            # Orden Limit ligeramente dentro del spread (precio un 0.02% más agresivo)
            offset = current_price * 0.0002
            limit_price = _round_price(
                current_price - offset if side == "BUY" else current_price + offset,
                info["tick_size"]
            )
            qty = _calc_quantity(balance, RISK_PER_TRANCHE, current_price, info["step_size"], sl_pct)

            if DRY_RUN:
                log.info(f"[DRY_RUN] TRAMO 2 LIMIT {side} {qty} {symbol} @ {limit_price}")
                tranche2_price = limit_price
            else:
                try:
                    order = client.futures_create_order(
                        symbol=symbol,
                        side=side,
                        type="LIMIT",
                        timeInForce="GTC",
                        quantity=qty,
                        price=limit_price
                    )
                    tranche2_price = float(order.get("price", limit_price))
                    log.info(f"Tramo 2 LIMIT colocado: {order['orderId']} @ {tranche2_price}")
                except BinanceAPIException as e:
                    log.error(f"Error colocando LIMIT DCA: {e}")
                    return {"status": "error", "reason": str(e)}

            # Actualizar posición con precio promedio
            pos["entries"].append(tranche2_price)
            avg_entry = sum(pos["entries"]) / len(pos["entries"])
            pos["tranche"] = 2
            log.info(f"Precio promedio actualizado: {avg_entry:.4f}")

            # Cancelar SL/TP anterior y reposicionar
            _cancel_open_sl_tp(client, symbol, pos)
            orders = _place_sl_tp(client, symbol, side, qty * 2, avg_entry, sl_pct, tp_pct, info["tick_size"])
            pos.update(orders)
            active_positions[symbol] = pos

            return {"status": "dca_tranche2", "symbol": symbol, "avg_entry": avg_entry}

        else:
            # Señal en dirección OPUESTA: ignorar (no contra-operar una posición abierta)
            log.info(f"Señal opuesta a posición abierta en {symbol}. Ignorada.")
            return {"status": "ignored", "reason": "opposite_direction"}

    # 4. SIN posición: TRAMO 1 con MARKET
    log.info(f"Nueva posición: TRAMO 1 MARKET {side} {symbol}...")
    
    info = _get_symbol_info(client, symbol)
    if info is None:
        return {"status": "error", "reason": "no_symbol_info"}

    balance = _get_balance(client)
    current_price = info["current_price"] or 0
    qty = _calc_quantity(balance, RISK_PER_TRANCHE, current_price or 1, info["step_size"], sl_pct)

    if DRY_RUN:
        exec_price = current_price or 99999.0
        log.info(f"[DRY_RUN] TRAMO 1 MARKET {side} {qty} {symbol} @ ~{exec_price}")
    else:
        try:
            order = client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=qty
            )
            # En Market, la ejecución puede ser en varios fills; tomamos el precio del ticket
            exec_price = float(order.get("avgPrice", current_price))
            log.info(f"Tramo 1 MARKET ejecutado: {order['orderId']} @ {exec_price}")
        except BinanceAPIException as e:
            log.error(f"Error colocando MARKET: {e}")
            return {"status": "error", "reason": str(e)}

    # Colocar SL y TP
    orders = _place_sl_tp(client, symbol, side, qty, exec_price, sl_pct, tp_pct, info["tick_size"])

    # Registrar posición activa
    active_positions[symbol] = {
        "side":    side,
        "entries": [exec_price],
        "tranche": 1,
        **orders
    }

    log.info(f"Posicion registrada: {symbol} {side} | Tramo: 1 | Precio: {exec_price}")
    return {"status": "opened", "symbol": symbol, "side": side, "entry": exec_price}


def close_position(symbol: str, reason: str = "manual"):
    """Cierra y elimina del registro una posición abierta (llamado por SL/TP callback o manual)."""
    if symbol in active_positions:
        del active_positions[symbol]
        log.info(f"Posicion {symbol} cerrada y liberada ({reason})")


def status():
    """Devuelve el estado actual de todas las posiciones abiertas."""
    if not active_positions:
        log.info("No hay posiciones abiertas.")
    for sym, pos in active_positions.items():
        avg = sum(pos["entries"]) / len(pos["entries"])
        log.info(f"{sym} | {pos['side']} | Tramo {pos['tranche']} | Avg Entry: {avg:.4f}")
    return active_positions


# ============================================================
# PUNTO DE ENTRADA LOCAL (para prueba rápida)
# ============================================================
if __name__ == "__main__":
    print(f"Modo: {'DRY_RUN' if DRY_RUN else ('TESTNET' if USE_TESTNET else 'REAL')}")
    print("Enviando señal de prueba...\n")
    
    resultado = process_signal({
        "symbol": "BTCUSDT",
        "side": "BUY",
        "verdict": "ALTA PROBABILIDAD",
        "tp_pct": 0.0075,
        "sl_pct": 0.0025,
        "score": 9
    })
    print(f"\nResultado: {json.dumps(resultado, indent=2)}")
    
    print("\nSimulando segunda señal DCA en la misma direccion...")
    resultado2 = process_signal({
        "symbol": "BTCUSDT",
        "side": "BUY",
        "verdict": "ALTA PROBABILIDAD",
        "tp_pct": 0.0075,
        "sl_pct": 0.0025,
        "score": 9
    })
    print(f"\nResultado DCA: {json.dumps(resultado2, indent=2)}")
    
    print("\nEstado actual de posiciones:")
    status()
