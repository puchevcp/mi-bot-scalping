from fastapi import FastAPI, Request
import uvicorn
from pydantic import BaseModel
from typing import Optional
from telegram_notifier import send_telegram_message
import asyncio
import aiohttp
import os
from datetime import datetime
from journal_manager import log_signal, simulate_trade, get_now_utc3

# Import the existing variables and start functions from our data engine
from binance_data import (
    ctx, listen_trades, SPOT_WS_URL, FUTURES_WS_URL, SYMBOL,
    listen_local_orderbook, listen_liquidations, fetch_oi_loop, display_context
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
    print("[*] Iniciando Motores de Order Flow Avanzados...")
    asyncio.create_task(listen_trades(SPOT_WS_URL, is_spot=True))
    asyncio.create_task(listen_trades(FUTURES_WS_URL, is_spot=False))
    asyncio.create_task(listen_local_orderbook())
    asyncio.create_task(listen_liquidations())
    asyncio.create_task(fetch_oi_loop())
    asyncio.create_task(display_context())
import math
from datetime import datetime, timezone

def analyze_structure(data, is_buy_signal, tf_name) -> tuple[bool, str, float]:
    if not isinstance(data, list) or len(data) < 11: 
        return False, f"Pocos datos ({tf_name})", 1.0
        
    # Extraer ultimas 4 velas para la estructura
    struct_candles = data[-4:]
    bull_vol = 0.0
    bear_vol = 0.0
    candles = []
    
    for c in struct_candles:
        o, h, l, close, v = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])
        candles.append({"open": o, "high": h, "low": l, "close": close, "vol": v})
        if close > o: bull_vol += v
        else: bear_vol += v
        
    # Calcular RVOL (Volumen de la ultima vela vs promedio de las 10 anteriores)
    vol_actual = candles[-1]["vol"]
    vols_hist = [float(c[5]) for c in data[-11:-1]]
    vol_promedio = sum(vols_hist) / len(vols_hist)
    rvol = vol_actual / vol_promedio if vol_promedio > 0 else 1.0
    
    r_ok = (rvol >= 1.2)
    rvol_tag = f" ⭐ (RVOL {rvol:.1f})" if r_ok else f" (RVOL {rvol:.1f})"
    
    if is_buy_signal:
        if candles[3]["low"] >= candles[1]["low"] * 0.999: # HL o plano
            if bull_vol > bear_vol: return True, f"Alcista (HL) + Vol. Compra{rvol_tag}", rvol
            else: return False, f"Sin Vol. Comprador{rvol_tag}", rvol
        else: return False, f"Rompiendo a la Baja{rvol_tag}", rvol
    else:
        if candles[3]["high"] <= candles[1]["high"] * 1.001: # LH o plano
            if bear_vol > bull_vol: return True, f"Bajista (LH) + Vol. Venta{rvol_tag}", rvol
            else: return False, f"Sin Vol. Vendedor{rvol_tag}", rvol
        else: return False, f"Rompiendo al Alza{rvol_tag}", rvol

def calculate_vwap(data):
    if not isinstance(data, list): return None, None
    now_utc = datetime.now(timezone.utc)
    start_of_day_ts = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc).timestamp() * 1000
    
    todays_candles = [c for c in data if c[0] >= start_of_day_ts]
    if not todays_candles: return None, None
    
    cum_vol = 0.0
    cum_vol_price = 0.0
    for c in todays_candles:
        h, l, close, vol = float(c[2]), float(c[3]), float(c[4]), float(c[5])
        typ_price = (h + l + close) / 3.0
        cum_vol += vol
        cum_vol_price += typ_price * vol
        
    vwap = cum_vol_price / cum_vol if cum_vol > 0 else 0
    
    variances = []
    for c in todays_candles:
        h, l, close, vol = float(c[2]), float(c[3]), float(c[4]), float(c[5])
        typ_price = (h + l + close) / 3.0
        variances.append((vol / cum_vol) * ((typ_price - vwap) ** 2))
        
    stdev = math.sqrt(sum(variances))
    return vwap, stdev

def calculate_atr(data, period=14):
    if not isinstance(data, list) or len(data) < period + 1: return 0
    tr_list = []
    for i in range(1, len(data)):
        h, l, prev_c = float(data[i][2]), float(data[i][3]), float(data[i-1][4])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def calculate_mfi(data, period=60):
    if not isinstance(data, list) or len(data) < period + 1: return None, None
    
    # Formula VMC: SMA(((Close - Open) / (High - Low)) * 150, period) - 2.5
    mfi_values = []
    for c in data:
        o, h, l, close = float(c[1]), float(c[2]), float(c[3]), float(c[4])
        # Evitar division por cero si high == low
        raw_mfi = (((close - o) / (h - l)) * 150) if (h - l) != 0 else 0
        mfi_values.append(raw_mfi - 2.5)
    
    # Calcular SMA de los ultimos 'period' valores para el actual y el anterior
    current_mfi_sma = sum(mfi_values[-period:]) / period
    prev_mfi_sma = sum(mfi_values[-(period+1):-1]) / period
    
    return current_mfi_sma, prev_mfi_sma

def calculate_wt(data, chlen=12, avglen=3):
    if not isinstance(data, list) or len(data) < 20: return 0, 0
    df = pd.DataFrame([row[:6] for row in data], columns=["ts","open","high","low","close","vol"])
    for col in ["open","high","low","close","vol"]: df[col] = df[col].astype(float)
    
    hlc3 = (df['high'] + df['low'] + df['close']) / 3.0
    esa = hlc3.ewm(span=chlen, adjust=False).mean()
    de = (hlc3 - esa).abs().ewm(span=chlen, adjust=False).mean()
    ci = (hlc3 - esa) / (0.015 * de)
    wt1 = ci.ewm(span=avglen, adjust=False).mean()
    wt2 = wt1.rolling(window=4).mean()
    return wt1.iloc[-1], wt2.iloc[-1]

import pandas as pd

async def fetch_kline(session, url):
    async with session.get(url, timeout=5) as resp:
        return await resp.json()

async def get_multiframe_context(symbol: str, is_buy_signal: bool):
    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=3m&limit=1000", # Mayor historico para EWMA (TradingView match)
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=20",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=1000",
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=5m&limit=288"
    ]
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            tasks = [fetch_kline(session, u) for u in urls]
            raw_3m, raw_5m, raw_15m, raw_vwap = await asyncio.gather(*tasks)
            
            # Las alertas de TradingView se ejecutan "Al Cierre" (On Bar Close).
            # Por lo tanto, la última vela de la API de Binance ya es la vela cerrada que disparó la señal.
            res_3m = raw_3m
            res_5m = raw_5m
            res_15m = raw_15m
            res_vwap = raw_vwap
            
            align_3m, msg_3m, r3 = analyze_structure(res_3m, is_buy_signal, "3m")
            align_5m, msg_5m, r5 = analyze_structure(res_5m, is_buy_signal, "5m")
            
            # EL FILTRO MACRO HIBRIDO (WaveTrend + Price Action)
            # Primero: ¿Como estan las velas?
            pa_aligned, pa_msg, _ = analyze_structure(res_15m, is_buy_signal, "15m")
            
            # Segundo: ¿Como esta el WaveTrend?
            wt1_15m, wt2_15m = calculate_wt(res_15m)
            wt_aligned = False
            if is_buy_signal and wt1_15m > wt2_15m: wt_aligned = True
            if not is_buy_signal and wt1_15m < wt2_15m: wt_aligned = True
            
            # El veredicto Macro es Verdadero si CUALQUIERA de los dos es positivo.
            # (Si el precio rompe arriba, no importa si el WT esta cruzando en el techo, es alcista).
            macro_aligned = pa_aligned or wt_aligned
            
            # Detectamos la tendencia REAL (no la alineacion) para el mensaje:
            # (Si el precio de ahora es mayor al de hace 5 velas (1h 15m), es Alcista)
            raw_pa_bullish = float(res_15m[-1][4]) > float(res_15m[-5][4])
            status_pa = "Alcista" if raw_pa_bullish else "Bajista"
            status_wt = "Alcista" if wt1_15m > wt2_15m else "Bajista"
            msg_15m = f"Precio {status_pa} | WT {status_wt} ({wt1_15m:.1f})"
            
            vwap, stdev = calculate_vwap(res_vwap)
            
            # MFI SINCRONIZADO AL GATILLO (3m):
            # Cambiamos de res_vwap (5m) a res_3m para que coincida con el grafico de 3m del usuario.
            mfi_now, mfi_prev = calculate_mfi(res_3m)
            
            atr_3m = calculate_atr(res_3m)
            wt1_val, wt2_val = calculate_wt(res_3m)
            
            return align_3m, msg_3m, align_5m, msg_5m, macro_aligned, msg_15m, vwap, stdev, mfi_now, mfi_prev, r3, wt1_val, atr_3m
    except asyncio.TimeoutError:
        return False, "Timeout", False, "Timeout", False, "Timeout", None, None, None, None, 1.0, 0, 0
    except Exception as e:
        print(f"[!] Error multiframe: {e}")
        return False, "Error API", False, "Error API", False, "Error API", None, None, None, None, 1.0, 0, 0
            
@app.post("/webhook")
async def receive_webhook(request: Request):
    try:
        data = await request.json()
        alert = TVAlert(**data)
        print(f"\n[!] Webhook recibido: {alert.signal} en {alert.pair}")
        
        is_buy_signal = "COMPRA" in alert.signal.upper() or "BUY" in alert.signal.upper()
        is_sell_signal = "VENTA" in alert.signal.upper() or "SELL" in alert.signal.upper()
        
        # Extraer variables del contexto GLOBAL
        cvd_spot = ctx.spot_cvd
        cvd_fut = ctx.futures_cvd
        oi_current = ctx.oi_current
        oi_prev = ctx.oi_5m_ago
        
        oi_delta_5m = 0.0
        if oi_prev > 0:
            oi_delta_5m = ((oi_current - oi_prev) / oi_prev) * 100
            
        m_price = 0
        m_vol = 0
        if ctx.heatmap_walls:
            # Heatmap walls son una tupla de (price, qty, type)
            m_price, m_vol, _ = ctx.heatmap_walls[0]

        (align_3m, msg_3m, align_5m, msg_5m, macro_aligned, msg_15m, 
         vwap, stdev, mfi_now, mfi_prev, rvol_3m, wt1_3m, atr_3m) = await get_multiframe_context(SYMBOL.upper(), is_buy_signal)
        
        # --- SECCIÓN DE PROCESAMIENTO GENERAL ---
        mfi_slope_ok = False
        if mfi_now is not None and mfi_prev is not None:
            if is_buy_signal and mfi_now > mfi_prev: mfi_slope_ok = True
            if is_sell_signal and mfi_now < mfi_prev: mfi_slope_ok = True
        
        loc_ok = False
        if is_buy_signal and wt1_3m <= -35: loc_ok = True
        if is_sell_signal and wt1_3m >= 35: loc_ok = True
        
        # --- CONDICIÓN PLATINUM (ELITE) ---
        # Ahora requiere CVD Futuros masivo (>30M) para ser considerado Platinum
        is_platinum = False
        cvd_fut_elite = abs(cvd_fut) > 30000000
        if align_3m and macro_aligned and mfi_slope_ok and loc_ok and rvol_3m >= 1.2 and cvd_fut_elite:
            is_platinum = True

        tp1_pct = (1.5 * atr_3m / alert.price) * 100 if alert.price > 0 else 0
        tp2_pct = (2.8 * atr_3m / alert.price) * 100 if alert.price > 0 else 0
        tp3_pct = (3.8 * atr_3m / alert.price) * 100 if alert.price > 0 else 0
        
        tp1 = alert.price * (1 + tp1_pct/100) if is_buy_signal else alert.price * (1 - tp1_pct/100)
        tp2 = alert.price * (1 + tp2_pct/100) if is_buy_signal else alert.price * (1 - tp2_pct/100)
        tp3 = alert.price * (1 + tp3_pct/100) if is_buy_signal else alert.price * (1 - tp3_pct/100)

        # --- SISTEMA DE PUNTUACIÓN (SCORING) ---
        ms15_pts = 1 if macro_aligned else 0
        ms3_pts = 1 if align_3m else 0
        mfi_pts = 2 if mfi_slope_ok else 0
        loc_pts = 1 if loc_ok else 0
        rvol_pts = 1 if rvol_3m >= 1.2 else 0
        spot_pts = 2 if (abs(cvd_spot) > 1000000 and ((is_buy_signal and cvd_spot > 0) or (not is_buy_signal and cvd_spot < 0))) else 0
        fut_pts = 2 if (abs(cvd_fut) > 5000000 and ((is_buy_signal and cvd_fut > 0) or (not is_buy_signal and cvd_fut < 0))) else 0
        oi_pts = 1 if (abs(oi_delta_5m) > 0.03 and ((is_buy_signal and oi_delta_5m > 0) or (not is_buy_signal and oi_delta_5m < 0))) else 0
        
        # Bono Institucional: +2 puntos si hay presion masiva (>50M)
        inst_bonus = 2 if abs(cvd_fut) > 50000000 else 0
        
        total_score = ms15_pts + ms3_pts + mfi_pts + loc_pts + rvol_pts + spot_pts + fut_pts + oi_pts + inst_bonus
        
        # Nombres de Veredicto segun Score
        if is_platinum:
            ver_name = "💎 PLATINUM SNIPER"
            total_score = max(total_score, 11)
        elif total_score >= 8: ver_name = "🟢 ALTA PROBABILIDAD"
        elif total_score >= 5: 
            # Restriccion de SHORTs en Probabilidad Media (Audit: 31.8% WR)
            if is_sell_signal and cvd_fut > 0:
                ver_name = "🔴 BAJA (SHORT CONTRA-TREND)"
                total_score = 4
            else:
                ver_name = "🟡 PROBABILIDAD MEDIA"
        else: ver_name = "🔴 BAJA PROBABILIDAD"
        
        verdict = f"{ver_name} ({total_score}/13)" # Aumentamos base a 13 por el bono
        if total_score < 4 and not is_platinum: 
            verdict = f"⚠️ TRAMPA PROBABLE ({total_score}/13)"

        # --- CONSTRUCCIÓN DE EMOJIS Y TEXTOS ---
        spot_check = "✅" if spot_pts > 0 else "❌"
        fut_check = "✅" if fut_pts > 0 else "❌"
        oi_check = "✅" if oi_pts > 0 else "❌"
        ms_3_check = "✅" if align_3m else "❌"
        ms_15_check = "✅" if macro_aligned else "❌"
        mfi_check = "✅" if mfi_slope_ok else "❌"
        
        mfi_emoji = "➖"
        if mfi_now is not None and mfi_prev is not None:
            mfi_emoji = "📈" if mfi_now > mfi_prev else "📉"
        
        spot_txt = "Alcista" if cvd_spot > 0 else "Bajista"
        fut_txt = "Alcista" if cvd_fut > 0 else "Bajista"
        oi_txt = "Alcista" if oi_delta_5m > 0 else "Bajista"

        vwap_msg = "No data"
        if vwap:
            if (is_buy_signal and alert.price > vwap) or (is_sell_signal and alert.price < vwap):
                vwap_msg = f"✅ Precio ARRIBA del VWAP" if is_buy_signal else f"✅ Precio DEBAJO del VWAP"
            else:
                vwap_msg = f"❌ Precio contra el VWAP"

        # --- CONSTRUCCIÓN DEL MENSAJE TELEGRAM ---
        # Definir emojis para la señal
        s_emoji = "🟢🟢" if is_buy_signal else "🔴🔴"
        s_action = "COMPRA" if is_buy_signal else "VENTA"
        
        msg_telegram = (
            f"🚨 <b>SNIPER ELITE 3</b> 🚨\n\n"
            f"Señal: {s_emoji} {s_action} - GATILLO MARKET CIPHER B (${alert.price:,.2f})\n"
            f"Veredicto: <b>{verdict}</b>\n\n"
            f"🎯 <b>OBJETIVOS SUGERIDOS (ATR Dynamic)</b>\n"
            f"┣ 🟢 TP 1 (Safe): +{tp1_pct:.2f}% (${tp1:,.1f})\n"
            f"┣ 💎 TP 2 (Platinum): +{tp2_pct:.2f}% (${tp2:,.1f})\n"
            f"┗ 🚀 TP 3 (Moon): +{tp3_pct:.2f}% (${tp3:,.1f})\n"
            f"<i>SL recomendado: -0.25% fijos.</i>\n\n"
            f"📊 <b>ORDEN FLOW & ESTRUCTURA</b>\n"
            f"┣ [{ms_15_check}] Estructura 15m (1pt): {msg_15m}\n"
            f"┣ [{ms_3_check}] Estructura 3m (1pt): {msg_3m} (RVOL {rvol_3m:.1f})\n"
            f"┣ [{mfi_check}] MFI Flow (2pts): {mfi_now:.1f} ({mfi_emoji})\n"
            f"┣ [✅] Zona W.T (1pt): {wt1_3m:.1f}\n"
            f"┣ [{spot_check}] CVD Spot (2pts): ${cvd_spot:,.0f} ({spot_txt})\n"
            f"┣ [{fut_check}] CVD Futuros (2pts): ${cvd_fut:,.0f} ({fut_txt})\n"
            f"┣ [{oi_check}] Delta OI (5m/1pt): {oi_delta_5m:+.3f}% ({oi_txt})\n"
            f"┗ Muro Heatmap: {m_vol:,.0f} BTC en ${m_price:,.0f}\n\n"
            f"<i>Nota: {vwap_msg}. Revisar <a href='https://aggr.trade/'>Aggr.trade</a>.</i>"
        )
        
        # --- NUEVA SECCIÓN: BITÁCORA Y SIMULACIÓN ---
        timestamp_id = get_now_utc3().strftime("%Y-%m-%d %H:%M:%S")
        sl_val = alert.price * (1 - 0.0025) if is_buy_signal else alert.price * (1 + 0.0025)
        
        journal_entry = {
            "Timestamp": timestamp_id,
            "Signal": alert.signal,
            "Pair": alert.pair,
            "Price": alert.price,
            "Verdict": ver_name,
            "Score": f"{total_score}/11",
            "MFI_Val": round(mfi_now, 2),
            "CVD_Spot": int(cvd_spot),
            "CVD_Fut": int(cvd_fut),
            "OI_Delta": f"{oi_delta_5m:.4f}%", # Quitamos el + para evitar error de formula
            "RVOL": round(rvol_3m, 2),
            "WT_Zone": round(wt1_3m, 2),
            "Structure_15m": msg_15m,
            "Structure_3m": msg_3m,
            "TP1": tp1,
            "TP2": tp2,
            "TP3": tp3,
            "SL": sl_val,
            "Result": "OPEN",
            "Close_Price": 0,
            "Exit_Time": ""
        }
        
        try:
            log_signal(journal_entry)
            # Iniciar monitoreo en segundo plano para ver si toca TP o SL
            asyncio.create_task(simulate_trade(
                timestamp_id, alert.pair, alert.price, 
                tp1, tp2, tp3, sl_val, is_buy_signal
            ))
        except Exception as logger_err:
            print(f"[!] Error al loguear/simular: {logger_err}")

        asyncio.create_task(send_telegram_message(msg_telegram))
        return {"status": "success", "verdict": verdict}
    except Exception as e:
        print(f"[!] ERROR EN WEBHOOK: {e}")
        return {"status": "error", "message": str(e)}

@app.api_route("/", methods=["GET", "HEAD"])
async def health_check():
    return {"status": "online", "service": "Platinum Sniper Engine"}

if __name__ == "__main__":
    import platform
    if platform.system() == 'Windows':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Render asigna un puerto dinámico mediante la variable de entorno 'PORT'
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
