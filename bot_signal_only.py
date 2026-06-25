"""
ANAYA FUTUROS — Trading Bot Server (SIGNAL MODE ONLY)
Detecta señales y manda alertas a Telegram SIN ejecutar órdenes
Corre en Railway.com 24/7
"""

import os, time, json, math, threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests as req
from binance.client import Client as BinanceClient

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
BOT_TOKEN  = os.environ.get('BOT_TOKEN', 'anaya2024')
# Hardcodear para debuggear - temporal
TELEGRAM_TOKEN = os.environ.get('TG_BOT_TOKEN', '') or '8995230957:AAFBEZ0syxY1PQ-aDYmraR7DeyvwWaivuW8'
TELEGRAM_CHAT  = os.environ.get('TG_CHAT_ID', '') or '8765545609'

# Cliente de Binance - Se crea SOLO cuando sea necesario (lazy initialization)
binance_client = None

BASE_URL = 'https://fapi.binance.com'

# ══════════════════════════════════════════════════════════
# BOT STATE
# ══════════════════════════════════════════════════════════
bot_state = {
    'running': True,  # Inicia ACTIVO automáticamente
    'symbol': 'HYPEUSDT',
    'interval': '3m',
    'strategy': 'simple',
    'sl_pct': 0.8,
    'tp_pct': 2.0,
    'log': [],
    'last_tick': None,
    'status': 'ACTIVO 24/7',
    'signals_sent': 0
}

def add_log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    entry = {'time': ts, 'msg': msg, 'level': level}
    bot_state['log'].insert(0, entry)
    bot_state['log'] = bot_state['log'][:100]
    print(f"[{ts}] {msg}")
    if level == 'signal' and TELEGRAM_TOKEN and TELEGRAM_CHAT:
        send_telegram(msg)

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={'chat_id': TELEGRAM_CHAT,
                 'text': f"🤖 ANAYA FUTUROS\n{msg}"}, timeout=5)
        print(f"✅ Telegram enviado")
    except Exception as e:
        print(f"⚠️ Error Telegram: {e}")

# ══════════════════════════════════════════════════════════
# BINANCE API (solo lectura, sin órdenes)
# ══════════════════════════════════════════════════════════
def get_candles(symbol, interval, limit=200):
    """Obtiene velas de Binance usando python-binance OFICIAL"""
    try:
        # Crear cliente SOLO cuando sea necesario - tld='com' para versión global (no bloqueada)
        client = BinanceClient(tld='com')
        
        # Usar python-binance en lugar de requests HTTP
        klines = client.get_historical_klines(
            symbol, interval, f"{limit} hours ago UTC", limit=limit
        )
        
        if not klines:
            return []
        
        # Convertir formato
        candles = []
        for k in klines:
            candles.append({
                't': k[0],
                'o': float(k[1]),
                'h': float(k[2]),
                'l': float(k[3]),
                'c': float(k[4]),
                'v': float(k[7])
            })
        return candles
    except Exception as e:
        add_log(f"⚠️ Error obteniendo velas: {e}", 'warn')
        return []

def get_price(symbol):
    """Obtiene precio actual de Binance"""
    try:
        # Crear cliente SOLO cuando sea necesario - tld='com' para versión global (no bloqueada)
        client = BinanceClient(tld='com')
        
        ticker = client.get_symbol_ticker(symbol=symbol)
        if ticker:
            return float(ticker['price'])
    except Exception as e:
        add_log(f"⚠️ Error obteniendo precio: {e}", 'warn')
    return 0

# ══════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════
def rsi(candles, period=14):
    closes = [c['c'] for c in candles]
    g, l = 0, 0
    result = [None]
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if i <= period:
            g += max(diff,0)
            l += max(-diff,0)
            if i == period:
                ag, al = g/period, l/period
                result.append(100 if al==0 else 100-100/(1+ag/al))
            else:
                result.append(None)
        else:
            dg, dl = max(diff,0), max(-diff,0)
            g = g*(period-1)/period + dg/period
            l = l*(period-1)/period + dl/period
            result.append(100 if l==0 else 100-100/(1+g/l))
    return result

# ══════════════════════════════════════════════════════════
# TRENDLINE STRATEGY
# ══════════════════════════════════════════════════════════
def trendline_signal(candles, sl_pct, tp_pct, params=None):
    if params is None:
        params = {}
    
    try:
        LB = int(params.get('lbLines', 60))
        PLB = int(params.get('pivLB', 5))
        min_t = int(params.get('minT', 2))
        tol = float(params.get('tolPct', 0.004))
        vol_mult = float(params.get('volMult', 1.3))
        
        if len(candles) < LB + PLB*2:
            return None
        
        slice_c = candles[-LB:]
        cur = candles[-2]
        tol_val = tol
        last = len(slice_c) - 1
        rng = max(cur['h'] - cur['l'], 0.0001)
        
        # Volume filter
        vols = [c['v'] for c in candles[-21:-1]]
        avg_vol = sum(vols) / len(vols) if vols else 0
        vol_ok = avg_vol and cur['v'] > avg_vol * vol_mult
        
        # RSI filter
        rs = rsi(candles[-21:])
        rs_val = rs[-2] if len(rs) >= 2 else None
        
        def is_ph(arr, idx):
            h = arr[idx]['h']
            for k in range(max(0, idx-PLB), min(len(arr), idx+PLB+1)):
                if k != idx and arr[k]['h'] >= h:
                    return False
            return True
        
        def is_pl(arr, idx):
            l = arr[idx]['l']
            for k in range(max(0, idx-PLB), min(len(arr), idx+PLB+1)):
                if k != idx and arr[k]['l'] <= l:
                    return False
            return True
        
        phs = [{'idx':j,'p':slice_c[j]['h']} for j in range(PLB, last-PLB) if is_ph(slice_c,j)]
        pls = [{'idx':j,'p':slice_c[j]['l']} for j in range(PLB, last-PLB) if is_pl(slice_c,j)]
        
        # SHORT: bearish trendline
        for a in range(len(phs)-1):
            for b in range(a+1, len(phs)):
                p1, p2 = phs[a], phs[b]
                if p2['p'] >= p1['p']:
                    continue
                slope = (p2['p'] - p1['p']) / (p2['idx'] - p1['idx'])
                if abs(slope) / p1['p'] < 0.00015:
                    continue
                proj = p1['p'] + slope * (last - p1['idx'])
                touches = sum(1 for j in range(p1['idx'], last+1)
                             if abs(slice_c[j]['h'] - (p1['p'] + slope*(j-p1['idx']))) / (p1['p'] + slope*(j-p1['idx'])) < tol_val)
                if touches < min_t:
                    continue
                touching = abs(cur['h'] - proj) / proj < tol_val
                wick_rej = (cur['h'] - cur['c']) / rng > 0.003
                bearish = cur['c'] < cur['o']
                rsi_ok = rs_val is None or rs_val > 35
                if touching and wick_rej and bearish and vol_ok and rsi_ok:
                    return 'short'
        
        # LONG: bullish trendline
        for a in range(len(pls)-1):
            for b in range(a+1, len(pls)):
                p1, p2 = pls[a], pls[b]
                if p2['p'] <= p1['p']:
                    continue
                slope = (p2['p'] - p1['p']) / (p2['idx'] - p1['idx'])
                if abs(slope) / p1['p'] < 0.00015:
                    continue
                proj = p1['p'] + slope * (last - p1['idx'])
                touches = sum(1 for j in range(p1['idx'], last+1)
                             if abs(slice_c[j]['l'] - (p1['p'] + slope*(j-p1['idx']))) / (p1['p'] + slope*(j-p1['idx'])) < tol_val)
                if touches < min_t:
                    continue
                touching = abs(cur['l'] - proj) / proj < tol_val
                wick_rej = (cur['c'] - cur['l']) / rng > 0.003
                bullish = cur['c'] > cur['o']
                rsi_ok = rs_val is None or rs_val < 65
                if touching and wick_rej and bullish and vol_ok and rsi_ok:
                    return 'long'
        
        return None
    except Exception as e:
        add_log(f"⚠️ Trendline error: {e}", 'warn')
        return None

# ══════════════════════════════════════════════════════════
# EMA CROSS STRATEGY
# ══════════════════════════════════════════════════════════
def ema(values, period):
    """Calcula EMA"""
    if len(values) < period:
        return None
    ema_val = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for i in range(period, len(values)):
        ema_val = values[i] * multiplier + ema_val * (1 - multiplier)
    return ema_val

def simple_signal(candles, params=None):
    """ESTRATEGIA ULTRA SIMPLE: Solo cierre vs apertura"""
    if params is None:
        params = {}
    
    try:
        if len(candles) < 2:
            return None
        
        curr = candles[-1]
        close = float(curr['c'])
        open_p = float(curr['o'])
        
        # LONG: cierre > apertura (vela verde)
        if close > open_p:
            return 'long'
        
        # SHORT: cierre < apertura (vela roja)
        if close < open_p:
            return 'short'
        
        return None
    except Exception as e:
        add_log(f"⚠️ Simple signal error: {e}", 'warn')
        return None

def ema_cross_signal(candles, params=None):
    """EMA Cross - será agregado después"""
    return simple_signal(candles, params)

# ══════════════════════════════════════════════════════════
# BOT TICK
# ══════════════════════════════════════════════════════════
def bot_tick():
    if not bot_state['running']:
        return
    
    symbol = bot_state['symbol']
    sl_pct = bot_state['sl_pct'] / 100
    tp_pct = bot_state['tp_pct'] / 100
    
    add_log(f"🔍 Verificando señal [{symbol} {bot_state['interval']}]")
    bot_state['last_tick'] = datetime.now().strftime('%H:%M:%S')
    
    try:
        candles = get_candles(symbol, bot_state['interval'], 200)
        if not candles or len(candles) < 10:
            add_log("⚠️ No hay datos de velas", 'warn')
            return
        
        price = get_price(symbol)
        if price <= 0:
            add_log("⚠️ No se pudo obtener precio", 'warn')
            return
        
        # Get signal
        sig = None
        if bot_state['strategy'] == 'simple':
            params = bot_state.get('strategy_params', {})
            sig = simple_signal(candles, params)
        elif bot_state['strategy'] == 'ema_cross':
            params = bot_state.get('strategy_params', {})
            sig = ema_cross_signal(candles, params)
        elif bot_state['strategy'] == 'trendline_vol':
            params = bot_state.get('strategy_params', {})
            sig = trendline_signal(candles, sl_pct, tp_pct, params)
        
        if not sig:
            add_log('⏸ Sin señal')
            return
        
        # SIGNAL MODE: manda alert a Telegram
        sl_price = price * (1 - sl_pct) if sig == 'long' else price * (1 + sl_pct)
        tp_price = price * (1 + tp_pct) if sig == 'long' else price * (1 - tp_pct)
        
        msg = (
            f"📡 SEÑAL DETECTADA\n"
            f"Par: {symbol} | {bot_state['interval']}\n"
            f"Dirección: {'LONG 📈' if sig=='long' else 'SHORT 📉'}\n"
            f"Precio: ${price:.4f}\n"
            f"SL sugerido: ${sl_price:.4f} (-{sl_pct*100:.1f}%)\n"
            f"TP sugerido: ${tp_price:.4f} (+{tp_pct*100:.1f}%)\n"
            f"R:R 1:{tp_pct/sl_pct:.2f}\n\n"
            f"⚠️ SIGNAL MODE - Entra manualmente en Binance"
        )
        
        add_log(f"📡 SEÑAL: {sig.upper()} @ ${price:.4f}", 'signal')
        send_telegram(msg)
        bot_state['signals_sent'] += 1
        bot_state['status'] = f"Última señal: {'LONG' if sig=='long' else 'SHORT'} @ ${price:.4f}"
        
    except Exception as e:
        add_log(f"❌ Error tick: {e}", 'error')

# ══════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════
def get_tf_seconds(interval):
    return {'1m':60,'3m':180,'5m':300,'15m':900,'1h':3600,'4h':14400,'1d':86400}.get(interval,3600)

def scheduler():
    while True:
        if bot_state['running']:
            tf_sec = get_tf_seconds(bot_state['interval'])
            now = time.time()
            next_close = (int(now/tf_sec)+1)*tf_sec + 5
            wait = next_close - now
            add_log(f"⏰ [{bot_state['interval']}] Próxima vela en {int(wait//60)}m {int(wait%60)}s")
            time.sleep(max(wait, 10))
            if bot_state['running']:
                bot_tick()
        else:
            time.sleep(10)

# ══════════════════════════════════════════════════════════
# HTTP API SERVER
# ══════════════════════════════════════════════════════════
class BotHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json')
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type,Authorization')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS,HEAD')
        self.send_header('Access-Control-Allow-Headers','Content-Type,Authorization')
        self.end_headers()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers()

    def check_auth(self):
        token = self.headers.get('Authorization','').replace('Bearer ','')
        return token == BOT_TOKEN

    def do_GET(self):
        if self.path == '/ping':
            self.send_json({'ok':True,'status':'running','time':datetime.now().isoformat()})
            return

        if self.path == '/check-binance':
            # Debug endpoint - verifica datos reales de Binance
            try:
                symbol = 'HYPEUSDT'
                interval = '3m'
                
                # Crear cliente SOLO cuando sea necesario (tld='com' para versión global)
                client = BinanceClient(tld='com')
                klines = client.get_historical_klines(symbol, interval, "5 hours ago UTC", limit=5)
                
                if not klines:
                    self.send_json({'error': 'No data from Binance', 'symbol': symbol}, 500)
                    return
                
                # Última vela (puede estar incompleta)
                last = klines[-1]
                open_p = float(last[1])
                close = float(last[4])
                high = float(last[2])
                low = float(last[3])
                volume = float(last[7])
                
                # Detectar dirección
                if close > open_p:
                    direction = 'LONG 📈'
                    signal = 'long'
                elif close < open_p:
                    direction = 'SHORT 📉'
                    signal = 'short'
                else:
                    direction = 'NEUTRAL ➡️'
                    signal = None
                
                # Vela anterior para comparar
                prev = klines[-2]
                prev_close = float(prev[4])
                prev_open = float(prev[1])
                
                self.send_json({
                    'symbol': symbol,
                    'interval': interval,
                    'current_candle': {
                        'open': open_p,
                        'close': close,
                        'high': high,
                        'low': low,
                        'volume': volume,
                        'direction': direction,
                        'signal': signal,
                        'change_pct': round((close - open_p) / open_p * 100, 4)
                    },
                    'previous_candle': {
                        'open': prev_open,
                        'close': prev_close,
                        'direction': 'LONG 📈' if prev_close > prev_open else 'SHORT 📉'
                    },
                    'status': '✅ Binance conectado y leyendo datos',
                    'bot_strategy': bot_state['strategy']
                })
            except Exception as e:
                self.send_json({'error': str(e), 'status': '❌ Error conectando a Binance'}, 500)
            return
            # Test endpoint - envía mensaje a Telegram directamente
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {
                    'chat_id': TELEGRAM_CHAT,
                    'text': '✅ TEST: Anaya Futuros Bot está vivo y conectado a Telegram!'
                }
                r = req.post(url, json=payload, timeout=5)
                result = r.json()
                self.send_json({
                    'ok': result.get('ok', False),
                    'message_sent': result.get('ok', False),
                    'telegram_token_length': len(TELEGRAM_TOKEN),
                    'telegram_chat': TELEGRAM_CHAT,
                    'api_response': result
                })
            except Exception as e:
                self.send_json({'error': str(e), 'ok': False}, 500)
            return

        if self.path == '/debug-telegram':
            # Debug endpoint - no auth needed
            token_ok = len(TELEGRAM_TOKEN) > 20
            chat_ok = len(TELEGRAM_CHAT) > 5
            self.send_json({
                'telegram_token_present': token_ok,
                'telegram_chat_present': chat_ok,
                'telegram_token_length': len(TELEGRAM_TOKEN),
                'telegram_chat_length': len(TELEGRAM_CHAT),
                'telegram_token_starts_with': TELEGRAM_TOKEN[:20] if TELEGRAM_TOKEN else 'EMPTY',
                'telegram_chat_value': TELEGRAM_CHAT,
                'test_msg': 'Valores de Telegram configurados' if (token_ok and chat_ok) else 'FALTAN VALORES'
            })
            return

        if not self.check_auth():
            self.send_json({'error':'Unauthorized'},401)
            return

        if self.path == '/status':
            self.send_json({
                'running': bot_state['running'],
                'status': bot_state['status'],
                'symbol': bot_state['symbol'],
                'interval': bot_state['interval'],
                'strategy': bot_state['strategy'],
                'sl_pct': bot_state['sl_pct'],
                'tp_pct': bot_state['tp_pct'],
                'signals_sent': bot_state['signals_sent'],
                'last_tick': bot_state['last_tick'],
                'log': bot_state['log'][:20],
                'mode': 'SIGNAL ONLY'
            })
        else:
            self.send_json({'error':'Not found'},404)

    def do_POST(self):
        if not self.check_auth():
            self.send_json({'error':'Unauthorized'},401)
            return

        length = int(self.headers.get('Content-Length',0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if self.path == '/start':
            for k in ['symbol','interval','strategy','sl_pct','tp_pct']:
                if k in body:
                    try:
                        if k in ['sl_pct','tp_pct']:
                            bot_state[k] = float(body[k])
                        else:
                            bot_state[k] = body[k]
                    except:
                        pass
            
            strategy_params = {}
            for k in ['lbLines','pivLB','minT','tolPct','volMult']:
                if k in body:
                    try:
                        strategy_params[k] = float(body[k])
                    except:
                        pass
            if strategy_params:
                bot_state['strategy_params'] = strategy_params
            
            bot_state['running'] = True
            bot_state['status'] = 'ACTIVO 24/7'
            tf_secs = get_tf_seconds(bot_state['interval'])
            add_log(f"🚀 Bot CONFIGURADO: {bot_state['symbol']} {bot_state['interval']} | SL:{bot_state['sl_pct']}% TP:{bot_state['tp_pct']}%", 'signal')
            
            # Manda notificación a Telegram
            try:
                url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
                payload = {
                    'chat_id': TELEGRAM_CHAT,
                    'text': f"🤖 ANAYA FUTUROS\n✅ Bot ACTIVO 24/7\nPar: {bot_state['symbol']} | TF: {bot_state['interval']}\nEstrategia: {bot_state['strategy']}\nSL: {bot_state['sl_pct']}% | TP: {bot_state['tp_pct']}%"
                }
                req.post(url, json=payload, timeout=5)
            except:
                pass
            
            self.send_json({'ok':True,'msg':'Bot configurado y corriendo 24/7','status':'ACTIVO'})

        elif self.path == '/stop':
            bot_state['running'] = False
            bot_state['status'] = 'INACTIVO'
            add_log('⏹ Bot detenido', 'warn')
            self.send_json({'ok':True,'msg':'Bot detenido'})

        else:
            self.send_json({'error':'Not found'},404)

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 8080))
    add_log(f"🌐 ANAYA FUTUROS Bot [SIGNAL MODE] iniciando en puerto {PORT}")
    add_log(f"✅ Bot INICIANDO AUTOMÁTICAMENTE en modo 24/7")
    threading.Thread(target=scheduler, daemon=True).start()
    server = HTTPServer(('0.0.0.0', PORT), BotHandler)
    add_log(f"✅ Servidor listo. Modo: SOLO SEÑALES A TELEGRAM (24/7 ACTIVO)")
    server.serve_forever()
