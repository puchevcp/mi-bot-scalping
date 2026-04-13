import csv
import os
import asyncio
import aiohttp
from datetime import datetime

JOURNAL_FILE = "trades_journal.csv"
GOOGLE_SHEETS_URL = "https://script.google.com/macros/s/AKfycbzPR9Yzs-3hMET2rLGd9P5QEi2jONQVlHfsvltAUxNgIMtRwfcGOESXQnuD6x3fib07Rw/exec"

# Columnas de la Bitácora
COLUMNS = [
    "Timestamp", "Signal", "Pair", "Price", "Verdict", "Score",
    "MFI", "CVD_Spot", "CVD_Fut", "OI_Delta", "RVOL", "WT_Zone",
    "TP1", "TP2", "TP3", "SL", "Result", "Close_Price", "Exit_Time"
]

def init_journal():
    if not os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()

def log_signal(data):
    """Guarda una señal en la bitácora local (CSV) y en Google Sheets."""
    init_journal()
    with open(JOURNAL_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writerow(data)
    
    # Sincronizar con la nube
    asyncio.create_task(sync_to_sheets(data))

async def sync_to_sheets(data):
    """Envía los datos al Webhook de Google Apps Script."""
    if not GOOGLE_SHEETS_URL: return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(GOOGLE_SHEETS_URL, json=data) as resp:
                if resp.status == 200:
                    print(f"[OK] Sincronizado con Google Sheets")
                else:
                    print(f"[!] Error Sheets: Status {resp.status}")
    except Exception as e:
        print(f"[!] Error al sincronizar con Sheets: {e}")

async def simulate_trade(signal_id, pair, entry_price, tp1, tp2, tp3, sl, is_buy):
    """
    Simula el resultado del trade monitoreando el precio en tiempo real.
    signal_id: Puede ser el timestamp o un ID único para identificar la fila en el CSV.
    """
    print(f"[*] Iniciando Simulación para {pair} en ${entry_price:,.2f}...")
    
    start_time = datetime.now()
    max_duration_hours = 4 # Tiempo máximo de espera
    
    while True:
        try:
            # Obtener precio actual de Binance
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={pair.replace('.P', '')}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    res_data = await response.json()
                    curr_price = float(res_data['price'])
            
            # Verificar Stop Loss
            if is_buy and curr_price <= sl:
                update_journal_result(signal_id, "STOP LOSS (LOSS)", curr_price)
                break
            if not is_buy and curr_price >= sl:
                update_journal_result(signal_id, "STOP LOSS (LOSS)", curr_price)
                break
                
            # Verificar Take Profits
            # Para la bitácora, marcaremos como WIN si toca al menos el TP1
            if is_buy and curr_price >= tp1:
                res_txt = "TP1 HIT (WIN)"
                if curr_price >= tp2: res_txt = "TP2 HIT (WIN)"
                if curr_price >= tp3: res_txt = "TP3 HIT (WIN)"
                update_journal_result(signal_id, res_txt, curr_price)
                break
            
            if not is_buy and curr_price <= tp1:
                res_txt = "TP1 HIT (WIN)"
                if curr_price <= tp2: res_txt = "TP2 HIT (WIN)"
                if curr_price <= tp3: res_txt = "TP3 HIT (WIN)"
                update_journal_result(signal_id, res_txt, curr_price)
                break
            
            # Timeout (Cerrar por tiempo)
            elapsed = (datetime.now() - start_time).total_seconds() / 3600
            if elapsed >= max_duration_hours:
                update_journal_result(signal_id, "TIMEOUT (EXIT)", curr_price)
                break
                
            await asyncio.sleep(30) # Revisar cada 30 segundos
            
        except Exception as e:
            print(f"[!] Error en simulador: {e}")
            await asyncio.sleep(60)

def update_journal_result(timestamp_id, result_text, close_price):
    """Busca la entrada por timestamp y actualiza el resultado."""
    rows = []
    updated = False
    if not os.path.exists(JOURNAL_FILE): return
    
    with open(JOURNAL_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['Timestamp'] == timestamp_id:
                row['Result'] = result_text
                row['Close_Price'] = f"{close_price:,.2f}"
                row['Exit_Time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                updated = True
            rows.append(row)
            
    if updated:
        with open(JOURNAL_FILE, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        
        # Sincronizar actualización con la nube
        for r in rows:
            if r['Timestamp'] == timestamp_id:
                asyncio.create_task(sync_to_sheets(r))
                break
                
        print(f"[✓] Bitácora Actualizada: {timestamp_id} -> {result_text}")
