import aiohttp
import asyncio

# Token and Chat ID provided by the user
TELEGRAM_TOKEN = "8405777043:AAF35OTEDSTOM3B7FsUKVJVyFFbN04Wr7Po"
CHAT_ID = "1347866672"

async def send_telegram_message(text: str):
    """
    Sends an HTML formatted message to the user via Telegram.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    print(f"[!] Warning: error al enviar a Telegram -> {await response.text()}")
                    return False
                print("[*] Alerta enviada a Telegram con éxito.")
                return True
        except Exception as e:
            print(f"[!] Excepción al conectar con Telegram: {e}")
            return False

if __name__ == "__main__":
    # Test message to verify the connection
    test_message = (
        "🟢 <b>¡CONEXIÓN EXITOSA!</b>\n\n"
        "Soy tu nuevo asistente bot de scalping.\n"
        "A partir de ahora, te enviaré las notificaciones y señales "
        "directamente a este chat.\n\n"
        "<i>Monitoreando Binance Futures...</i> 🔎"
    )
    # Windows fix to run asyncio event loops reliably
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    asyncio.run(send_telegram_message(test_message))
