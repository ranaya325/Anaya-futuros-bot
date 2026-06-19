"""
ANAYA FUTUROS — Trading Bot Server
Corre en Render.com 24/7
Contrólalo desde la plataforma Anaya Futuros
"""

import os, time, hmac, hashlib, json, math, threading
from datetime import datetime
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests as req

# ══════════════════════════════════════════════════════════
# CONFIG — se ponen desde variables de entorno en Render
# ══════════════════════════════════════════════════════════
API_KEY    = os.environ.get('BINANCE_API_KEY', '')
API_SECRET = os.environ.get('BINANCE_SECRET', '')
BOT_TOKEN  = os.environ.get('BOT_TOKEN', 'anaya2024')  # token de seguridad
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT', '')

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
    'trade_size': 100,
    'leverage': 2,
    'position': None,   # {side, entry_price, qty, sl_price, tp_price}
    'session_pnl': 0.0,
    'trades_today': 0,
    'log': [],
    'last_tick': None,
    'status': 'INACTIVO'
}

def add_log(msg, level='info'):
    ts = datetime.now().strftime('%H:%M:%S')
    entry = {'time': ts, 'msg': msg, 'level': level}
    bot_state['log'].insert(0, entry)
    bot_state['log'] = bot_state['log'][:100]  # keep last 100
    print(f"[{ts}] {msg}")
    if level == 'trade' and TELEGRAM_TOKEN and TELEGRAM_CHAT:
        send_telegram(msg)

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        req.post(url, json={'chat_id': TELEGRAM_CHAT,
                 'text': f"🤖 ANAYA FUTUROS\n{msg}"}, timeout=5)
    except:
        pass

# ══════════════════════════════════════════════════════════
# BINANCE API
# ══════════════════════════════════════════════════════════
def sign(params):
    params['timestamp'] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig   = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + '&signature=' + sig

def binance_get(path, params=None):
    url = BASE_URL + path
    r = req.get(url, params=params, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
    return r.json()

def binance_post(path, params):
    url  = BASE_URL + path
    data = sign(params)
    r    = req.post(url, data=data,
           headers={'X-MBX-APIKEY': API_KEY,
                    'Content-Type': 'application/x-www-form-urlencoded'}, timeout=10)
    return r.json()

def binance_delete(path, params):
    url = BASE_URL + path
    r   = req.delete(url, params=sign(params),
          headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
    return r.json()

def get_candles(symbol, interval, limit=200):
    data = binance_get('/fapi/v1/klines', {
        'symbol': symbol, 'interval': interval, 'limit': limit
    })
    return [{'t':d[0],'o':float(d[1]),'h':float(d[2]),
             'l':float(d[3]),'c':float(d[4]),'v':float(d[5])} for d in data]

def get_price(symbol):
    d = binance_get('/fapi/v1/ticker/price', {'symbol': symbol})
    return float(d['price'])

def get_balance():
    data = binance_post('/fapi/v2/balance', {})
    usdt = next((b for b in data if b['asset']=='USDT'), None)
    return float(usdt['balance']) if usdt else 0

def set_leverage(symbol, lev):
    try:
        binance_post('/fapi/v1/leverage', {'symbol':symbol,'leverage':lev})
    except:
        pass

def place_market(symbol, side, qty):
    return binance_post('/fapi/v1/order', {
        'symbol':symbol,'side':side,'type':'MARKET','quantity':qty
    })

def place_sl(symbol, side, stop_price):
    try:
        binance_post('/fapi/v1/order', {
            'symbol':symbol,'side':side,'type':'STOP_MARKET',
            'stopPrice':str(round(stop_price,4)),'closePosition':'true'
        })
    except Exception as e:
        add_log(f"⚠ Error SL: {e}", 'warn')

def place_tp(symbol, side, stop_price):
    try:
        binance_post('/fapi/v1/order', {
            'symbol':symbol,'side':side,'type':'TAKE_PROFIT_MARKET',
            'stopPrice':str(round(stop_price,4)),'closePosition':'true'
        })
    except Exception as e:
        add_log(f"⚠ Error TP: {e}", 'warn')

def cancel_all_orders(symbol):
    try:
        binance_delete('/fapi/v1/allOpenOrders', {'symbol':symbol})
    except:
        pass

def get_symbol_info(symbol):
    """Returns (minQty, stepSize, precision) for a symbol"""
    try:
        info = binance_get('/fapi/v1/exchangeInfo')
        sym  = next(s for s in info['symbols'] if s['symbol']==symbol)
        lot  = next(f for f in sym['filters'] if f['filterType']=='LOT_SIZE')
        step = float(lot['stepSize'])
        minq = float(lot['minQty'])
        # Calculate decimal precision from stepSize
        s = lot['stepSize'].rstrip('0')
        prec = len(s.split('.')[-1]) if '.' in s else 0
        return minq, step, prec
    except Exception as e:
        add_log(f"⚠ get_symbol_info error: {e}", 'warn')
        return 1.0, 1.0, 1

def get_min_qty(symbol):
    minq, step, prec = get_symbol_info(symbol)
    return minq, step

def round_qty(qty, step, precision=None):
    """Round qty to stepSize with correct precision"""
    if step <= 0: return qty
    # Floor to nearest step
    result = math.floor(qty / step) * step
    # Determine decimal places from step
    if precision is None:
        s = f"{step:.10f}".rstrip('0')
        precision = len(s.split('.')[-1]) if '.' in s else 0
    return round(result, precision)

# ══════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════
def ema(closes, period):
    k = 2/(period+1)
    result, prev = [], None
    for i, v in enumerate(closes):
        if i < period-1: result.append(None); continue
        if prev is None: prev = sum(closes[:period])/period
        else: prev = v*k + prev*(1-k)
        result.append(prev)
    return result

def sma(values, period):
    return [None if i<period-1 else sum(values[i-period+1:i+1])/period
            for i in range(len(values))]

def rsi(candles, period=14):
    closes = [c['c'] for c in candles]
    g, l   = 0, 0
    result = [None]
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if i <= period:
            g += max(diff,0); l += max(-diff,0)
            if i == period:
                ag,al = g/period, l/period
                result.append(100 if al==0 else 100-100/(1+ag/al))
            else: result.append(None)
        else:
            dg,dl = max(diff,0), max(-diff,0)
            g = g*(period-1)/period + dg/period
            l = l*(period-1)/period + dl/period
            result.append(100 if l==0 else 100-100/(1+g/l))
    return result

# ══════════════════════════════════════════════════════════
# TRENDLINE STRATEGY
# ══════════════════════════════════════════════════════════
def trendline_signal(candles, sl_pct, tp_pct):
    LB, PLB = 60, 5
    if len(candles) < LB + PLB*2: return None
    
    slice_c = candles[-LB:]
    cur     = candles[-2]  # last CLOSED candle
    tol     = 0.004
    min_t   = 2
    last    = len(slice_c)-1
    rng     = max(cur['h']-cur['l'], 0.0001)

    # Volume filter
    vols = [c['v'] for c in candles[-21:-1]]
    avg_vol = sum(vols)/len(vols) if vols else 0
    vol_ok  = avg_vol and cur['v'] > avg_vol * 1.3

    # RSI filter
    rs = rsi(candles[-21:])
    rs_val = rs[-2] if len(rs)>=2 else None

    def is_ph(arr, idx):
        h = arr[idx]['h']
        for k in range(max(0,idx-PLB), min(len(arr),idx+PLB+1)):
            if k!=idx and arr[k]['h']>=h: return False
        return True

    def is_pl(arr, idx):
        l = arr[idx]['l']
        for k in range(max(0,idx-PLB), min(len(arr),idx+PLB+1)):
            if k!=idx and arr[k]['l']<=l: return False
        return True

    phs = [{'idx':j,'p':slice_c[j]['h']} for j in range(PLB, last-PLB) if is_ph(slice_c,j)]
    pls = [{'idx':j,'p':slice_c[j]['l']} for j in range(PLB, last-PLB) if is_pl(slice_c,j)]

    # SHORT: bearish trendline
    for a in range(len(phs)-1):
        for b in range(a+1, len(phs)):
            p1, p2 = phs[a], phs[b]
            if p2['p'] >= p1['p']: continue
            slope = (p2['p']-p1['p'])/(p2['idx']-p1['idx'])
            if abs(slope)/p1['p'] < 0.00015: continue
            proj  = p1['p'] + slope*(last-p1['idx'])
            touches = sum(1 for j in range(p1['idx'],last+1)
                         if abs(slice_c[j]['h']-(p1['p']+slope*(j-p1['idx'])))/(p1['p']+slope*(j-p1['idx'])) < tol)
            if touches < min_t: continue
            touching  = abs(cur['h']-proj)/proj < tol
            wick_rej  = (cur['h']-cur['c'])/rng > 0.003
            bearish   = cur['c'] < cur['o']
            rsi_ok    = rs_val is None or rs_val > 35
            if touching and wick_rej and bearish and vol_ok and rsi_ok:
                return 'short'

    # LONG: bullish trendline
    for a in range(len(pls)-1):
        for b in range(a+1, len(pls)):
            p1, p2 = pls[a], pls[b]
            if p2['p'] <= p1['p']: continue
            slope = (p2['p']-p1['p'])/(p2['idx']-p1['idx'])
            if abs(slope)/p1['p'] < 0.00015: continue
            proj  = p1['p'] + slope*(last-p1['idx'])
            touches = sum(1 for j in range(p1['idx'],last+1)
                         if abs(slice_c[j]['l']-(p1['p']+slope*(j-p1['idx'])))/(p1['p']+slope*(j-p1['idx'])) < tol)
            if touches < min_t: continue
            touching  = abs(cur['l']-proj)/proj < tol
            wick_rej  = (cur['c']-cur['l'])/rng > 0.003
            bullish   = cur['c'] > cur['o']
            rsi_ok    = rs_val is None or rs_val < 65
            if touching and wick_rej and bullish and vol_ok and rsi_ok:
                return 'long'

    return None

# ══════════════════════════════════════════════════════════
# EMA CROSS STRATEGY
# ══════════════════════════════════════════════════════════
def ema_cross_signal(candles, params=None):
    if params is None: params = {}
    eF  = int(params.get('eF', 9))
    eS  = int(params.get('eS', 21))
    rP  = int(params.get('rP', 14))
    rOB = float(params.get('rOB', 68))
    rOS = float(params.get('rOS', 32))
    closes = [c['c'] for c in candles]
    ef = ema(closes, eF)
    es = ema(closes, eS)
    rs = rsi(candles, rP)
    i  = len(candles)-2  # last closed candle
    if ef[i] is None or es[i] is None: return None
    if ef[i-1]<es[i-1] and ef[i]>es[i] and rs[i] and rs[i]<rOB: return 'long'
    if ef[i-1]>es[i-1] and ef[i]<es[i] and rs[i] and rs[i]>rOS: return 'short'
    return None

# ══════════════════════════════════════════════════════════
# BOT TICK — runs every candle close
# ══════════════════════════════════════════════════════════
def bot_tick():
    if not bot_state['running']:
        return

    symbol   = bot_state['symbol']
    sl_pct   = bot_state['sl_pct'] / 100
    tp_pct   = bot_state['tp_pct'] / 100
    strategy = bot_state['strategy']
    
    add_log(f"🔍 Verificando señal [{symbol} {bot_state['interval']}]")
    bot_state['last_tick'] = datetime.now().strftime('%H:%M:%S')

    try:
        candles = get_candles(symbol, bot_state['interval'], 200)
        price   = get_price(symbol)

        # ── Check open position ──────────────────────────
        pos = bot_state['position']
        if pos:
            is_long = pos['side'] == 'BUY'
            pct = ((price-pos['entry_price'])/pos['entry_price']
                   if is_long else
                   (pos['entry_price']-price)/pos['entry_price'])
            pnl_usdt = bot_state['trade_size'] * pct * bot_state['leverage']
            add_log(f"📊 Posición abierta: {'LONG' if is_long else 'SHORT'} | PnL: {pct*100:.2f}% | ${pnl_usdt:.2f}")
            # SL/TP already in Binance — just monitor
            return

        # ── Get signal ───────────────────────────────────
        sig = None
        if strategy == 'trendline_vol':
            sig = trendline_signal(candles, sl_pct, tp_pct)
        elif strategy == 'ema_cross':
            sig = ema_cross_signal(candles, bot_state.get('strategy_params', {}))

        if not sig:
            add_log('⏸ Sin señal')
            return

        # ── Open position ────────────────────────────────
        price   = get_price(symbol)
        min_qty, step, prec = get_symbol_info(symbol)
        size    = bot_state['trade_size'] * bot_state['leverage']
        raw_qty = size / price
        qty     = round_qty(raw_qty, step, prec)
        qty     = max(qty, min_qty)
        # Ensure qty meets minimum notional ($6+)
        while qty * price < 6.0:
            qty = round_qty(qty + step, step, prec)
        qty = round(qty, prec)  # final safety round
        add_log(f"💰 Precio: ${price:.4f} | Step: {step} | Prec: {prec} | Qty: {qty}", 'info')
        side    = 'BUY' if sig == 'long' else 'SELL'
        close_side = 'SELL' if side=='BUY' else 'BUY'

        add_log(f"📡 Señal: {sig.upper()} | ${price:.3f} | Qty: {qty}", 'trade')

        set_leverage(symbol, bot_state['leverage'])
        order = place_market(symbol, side, qty)

        if 'orderId' not in order:
            add_log(f"❌ Error al abrir: {order.get('msg','?')}", 'error')
            return

        fill_price = float(order.get('avgPrice', price))
        sl_price   = fill_price*(1-sl_pct) if side=='BUY' else fill_price*(1+sl_pct)
        tp_price   = fill_price*(1+tp_pct) if side=='BUY' else fill_price*(1-tp_pct)

        place_sl(symbol, close_side, sl_price)
        place_tp(symbol, close_side, tp_price)

        bot_state['position'] = {
            'side': side, 'entry_price': fill_price,
            'qty': qty, 'sl_price': sl_price, 'tp_price': tp_price,
            'open_time': datetime.now().isoformat()
        }
        bot_state['status'] = f"{'LONG' if side=='BUY' else 'SHORT'} abierto"
        add_log(f"✅ Orden abierta ID:{order['orderId']} Fill:${fill_price:.3f}", 'trade')
        add_log(f"🛡 SL:${sl_price:.3f} TP:${tp_price:.3f} (en Binance)", 'trade')

    except Exception as e:
        add_log(f"❌ Error tick: {e}", 'error')

# ══════════════════════════════════════════════════════════
# SCHEDULER — waits for candle close
# ══════════════════════════════════════════════════════════
def get_tf_seconds(interval):
    return {'1m':60,'3m':180,'5m':300,'15m':900,'1h':3600,'4h':14400,'1d':86400}.get(interval,3600)

def scheduler():
    while True:
        if bot_state['running']:
            tf_sec   = get_tf_seconds(bot_state['interval'])
            now      = time.time()
            # Wait until next candle close + 5 seconds buffer
            next_close = (int(now/tf_sec)+1)*tf_sec + 5
            wait = next_close - now
            add_log(f"⏰ [{bot_state['interval']}] Próxima vela en {int(wait//60)}m {int(wait%60)}s")
            time.sleep(max(wait, 10))
            if bot_state['running']:
                bot_tick()
        else:
            time.sleep(10)

# ══════════════════════════════════════════════════════════
# HTTP API SERVER — receives commands from Anaya Futuros UI
# ══════════════════════════════════════════════════════════
class BotHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # suppress server logs

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
        # UptimeRobot sends HEAD requests to check if server is alive
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers()

    def check_auth(self):
        token = self.headers.get('Authorization','').replace('Bearer ','')
        return token == BOT_TOKEN

    def do_GET(self):
        # /ping is public - no auth needed (for UptimeRobot)
        if self.path == '/ping':
            self.send_json({'ok':True,'status':'running','time':datetime.now().isoformat()})
            return

        if not self.check_auth():
            self.send_json({'error':'Unauthorized'},401); return

        if self.path == '/status':
            pos = bot_state['position']
            price = 0
            try: price = get_price(bot_state['symbol'])
            except: pass
            pnl_pct = 0
            if pos and price:
                is_long = pos['side']=='BUY'
                pnl_pct = ((price-pos['entry_price'])/pos['entry_price']
                           if is_long else
                           (pos['entry_price']-price)/pos['entry_price'])*100
            self.send_json({
                'running':      bot_state['running'],
                'status':       bot_state['status'],
                'symbol':       bot_state['symbol'],
                'interval':     bot_state['interval'],
                'strategy':     bot_state['strategy'],
                'sl_pct':       bot_state['sl_pct'],
                'tp_pct':       bot_state['tp_pct'],
                'trade_size':   bot_state['trade_size'],
                'leverage':     bot_state['leverage'],
                'session_pnl':  bot_state['session_pnl'],
                'trades_today': bot_state['trades_today'],
                'last_tick':    bot_state['last_tick'],
                'position':     pos,
                'live_pnl_pct': round(pnl_pct,3),
                'live_price':   price,
                'log':          bot_state['log'][:20]
            })

        elif self.path == '/ping':
            self.send_json({'ok':True,'time':datetime.now().isoformat()})

        elif self.path == '/balance':
            try:
                bal = get_balance()
                self.send_json({'balance': bal})
            except Exception as e:
                self.send_json({'error':str(e)},400)
        else:
            self.send_json({'error':'Not found'},404)

    def do_POST(self):
        if not self.check_auth():
            self.send_json({'error':'Unauthorized'},401); return

        length = int(self.headers.get('Content-Length',0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        if self.path == '/start':
            # Update config from request
            for k in ['symbol','interval','strategy','sl_pct','tp_pct','trade_size','leverage']:
                if k in body: bot_state[k] = body[k]
            # Save all extra strategy params (eF, eS, rP, etc.)
            strategy_params = {}
            for k in ['eF','eS','rP','rOB','rOS','pivLB','minT','tolPct','vM','lbLines','rejPct']:
                if k in body: strategy_params[k] = body[k]
            if strategy_params:
                bot_state['strategy_params'] = strategy_params
            # Set API keys if provided
            global API_KEY, API_SECRET
            if body.get('api_key'):    API_KEY    = body['api_key']
            if body.get('api_secret'): API_SECRET = body['api_secret']
            bot_state['running'] = True
            bot_state['status']  = 'ACTIVO'
            tf_secs=get_tf_seconds(bot_state['interval'])
    add_log(f"🚀 Bot iniciado: {bot_state['symbol']} {bot_state['interval']} ({tf_secs}s) | SL:{bot_state['sl_pct']}% TP:{bot_state['tp_pct']}%", 'trade')
            # Run first tick immediately
            threading.Thread(target=bot_tick, daemon=True).start()
            self.send_json({'ok':True,'msg':'Bot iniciado'})

        elif self.path == '/stop':
            bot_state['running'] = False
            bot_state['status']  = 'INACTIVO'
            add_log('⏹ Bot detenido', 'warn')
            self.send_json({'ok':True,'msg':'Bot detenido'})

        elif self.path == '/close_position':
            pos = bot_state['position']
            if not pos:
                self.send_json({'error':'Sin posición abierta'},400); return
            try:
                cancel_all_orders(bot_state['symbol'])
                close_side = 'SELL' if pos['side']=='BUY' else 'BUY'
                order = place_market(bot_state['symbol'], close_side, pos['qty'])
                fill  = float(order.get('avgPrice', get_price(bot_state['symbol'])))
                is_l  = pos['side']=='BUY'
                pct   = ((fill-pos['entry_price'])/pos['entry_price']
                         if is_l else
                         (pos['entry_price']-fill)/pos['entry_price'])
                pnl   = bot_state['trade_size'] * pct * bot_state['leverage']
                bot_state['session_pnl']  += pnl
                bot_state['trades_today'] += 1
                bot_state['position'] = None
                bot_state['status']   = 'ACTIVO'
                add_log(f"🔴 Posición cerrada manual | PnL: {pct*100:.2f}% | ${pnl:.2f}", 'trade')
                self.send_json({'ok':True,'pnl_pct':pct*100,'pnl_usdt':pnl})
            except Exception as e:
                self.send_json({'error':str(e)},400)

        elif self.path == '/test':
            # Force test trade
            try:
                price = get_price(bot_state['symbol'])
                min_qty, step = get_min_qty(bot_state['symbol'])
                qty = round_qty(min_qty, step)
                set_leverage(bot_state['symbol'], 1)
                buy  = place_market(bot_state['symbol'], 'BUY',  qty)
                time.sleep(2)
                sell = place_market(bot_state['symbol'], 'SELL', qty)
                add_log(f"🧪 Test trade OK: BUY {buy.get('orderId')} → SELL {sell.get('orderId')}", 'trade')
                self.send_json({'ok':True,'buy_id':buy.get('orderId'),'sell_id':sell.get('orderId')})
            except Exception as e:
                self.send_json({'error':str(e)},400)
        else:
            self.send_json({'error':'Not found'},404)

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
if __name__ == '__main__':
    PORT = int(os.environ.get('PORT', 8080))
    add_log(f"🌐 Servidor ANAYA FUTUROS Bot iniciando en puerto {PORT}")
    # Start scheduler in background
    threading.Thread(target=scheduler, daemon=True).start()
    # Start HTTP server
    server = HTTPServer(('0.0.0.0', PORT), BotHandler)
    add_log(f"✅ Servidor listo. Esperando conexión desde Anaya Futuros...")
    server.serve_forever()
