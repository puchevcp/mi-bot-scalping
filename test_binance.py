import os
import asyncio
from dotenv import load_dotenv
from binance import AsyncClient
from binance.exceptions import BinanceAPIException

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_SECRET_KEY", "")

async def test_connection():
    print("--- INICIANDO TEST BINANCE TESTNET ---")
    client = None
    try:
        client = await AsyncClient.create(API_KEY, API_SECRET, testnet=True)
        print("1. Cliente creado con exito.")
        
        # Test Balance
        account = await client.futures_account_balance()
        balance = 0.0
        for asset in account:
            if asset["asset"] == "USDT":
                balance = float(asset["availableBalance"])
        print(f"2. Balance USDT disponible: {balance}")
        
        if balance <= 0:
            print("ERROR: Balance insuficiente.")
            return

        # Test Info
        info = await client.futures_exchange_info()
        step_size = None
        for s in info["symbols"]:
            if s["symbol"] == "BTCUSDT":
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        step_size = float(f["stepSize"])
                        break
                break
        print(f"3. Info BTCUSDT obtenida. Step size: {step_size}")

        ticker = await client.futures_symbol_ticker(symbol="BTCUSDT")
        current_price = float(ticker["price"])
        print(f"4. Precio actual BTCUSDT: {current_price}")

        # Probar orden market mínima permitida
        # En Binance Testnet, para BTCUSDT, el minimo nocional suele ser 100 o mas bajo. Pero probemos con 0.001 BTC.
        qty = 0.002
        print(f"5. Intentando abrir orden MARKET LONG por {qty} BTC (~{qty * current_price:.2f} USDT)...")
        try:
            order = await client.futures_create_order(
                symbol="BTCUSDT",
                side="BUY",
                type="MARKET",
                quantity=qty
            )
            print(f"   EXITO! Orden ID: {order.get('orderId')}")
            
            # Intentar LIMIT order TP
            tp_price = round(current_price * 1.05, 1) # +5%
            print(f"6. Intentando poner orden LIMIT SELL a {tp_price}...")
            tp_order = await client.futures_create_order(
                symbol="BTCUSDT",
                side="SELL",
                type="LIMIT",
                price=tp_price,
                quantity=qty,
                reduceOnly=True,
                timeInForce="GTC"
            )
            print(f"   EXITO! LIMIT Orden ID: {tp_order.get('orderId')}")
            
            # Limpiar
            print("7. Cerrando todo (limpieza)...")
            await client.futures_cancel_all_open_orders(symbol="BTCUSDT")
            await client.futures_create_order(
                symbol="BTCUSDT",
                side="SELL",
                type="MARKET",
                quantity=qty
            )
            print("   Limpieza completada.")
            
        except BinanceAPIException as e:
            print(f"   ERROR DE API EN ORDEN: {e.message if hasattr(e, 'message') else e}")
            
    except Exception as e:
        print(f"ERROR GENERAL: {e}")
    finally:
        if client:
            await client.close_connection()
    print("--- FIN DEL TEST ---")

if __name__ == "__main__":
    asyncio.run(test_connection())
