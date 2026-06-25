"""
ANAYA FUTUROS — Trading Bot Server (SIGNAL MODE ONLY)
Detecta señales y manda alertas a Telegram SIN ejecutar órdenes
Corre en Railway.com 24/7
"""

import os, time, json, math, threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests as req

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
BOT_TOKEN  = os.environ.get('BOT_TOKEN', 'anaya2024')
TELEGRAM_TOKEN = os.environ.get('TG_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TG_CHAT_ID', '')

BASE_URL = 'https://fapi.binance.com'

# ══════════════════════════════════════════════════════════
# BOT STATE
# ══════════════════════════════════════════════════════════
bot_state = {
    'running': False,
    'symbol': 'HYPEUSDT',
    'interval': '1h',
    'strategy': 'trendline_vol',
    'sl_pct': 2.2,
    'tp_pct': 6.0,
    'log': [],
    'last_tick': None,
    'status': 'INACTIVO',
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
def binance_get(path, params=None):
    try:
        url = BASE_URL + path
        r = req.get(url, params=params, timeout=10)
        return r.json()
    except Exception as e:
        add_log(f"⚠️ Binance error: {e}", 'warn')
        return None

def get_candles(symbol, interval, limit=200):
    try:
        data = binance_get('/fapi/v1/klines', {
            'symbol': symbol, 'interval': interval, 'limit': limit
        })
        if not data:
            return []
        return [{'t':d[0],'o':float(d[1]),'h':float(d[2]),
                 'l':float(d[3]),'c':float(d[4]),'v':float(d[5])} for d in data]
    except:
        return []

def get_price(symbol):
    try:
        d = binance_get('/fapi/v1/ticker/price', {'symbol': symbol})
        if d:
            return float(d['price'])
    except:
        pass
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
        if bot_state['strategy'] == 'trendline_vol':
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
            bot_state['status'] = 'ACTIVO'
            tf_secs = get_tf_seconds(bot_state['interval'])
            add_log(f"🚀 Bot iniciado: {bot_state['symbol']} {bot_state['interval']} | SL:{bot_state['sl_pct']}% TP:{bot_state['tp_pct']}%", 'signal')
            threading.Thread(target=bot_tick, daemon=True).start()
            self.send_json({'ok':True,'msg':'Bot iniciado en SIGNAL MODE'})

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
    threading.Thread(target=scheduler, daemon=True).start()
    server = HTTPServer(('0.0.0.0', PORT), BotHandler)
    add_log(f"✅ Servidor listo. Modo: SOLO SEÑALES A TELEGRAM")
    server.serve_forever()
