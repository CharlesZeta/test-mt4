import os
import json
import threading
import traceback
import time
import re
from datetime import datetime
from collections import deque
from flask import Flask, request, render_template_string, jsonify

app = Flask(__name__)

# ==================== 全局数据结构 ====================
MAX_HISTORY = 50
KLINE_MAX = {"5min": 300, "10min": 250, "1hour": 200}
kline_data = {}
kline_locks = {}

# ==================== 品种名归一化（核心修复） ====================
KNOWN_SYMBOLS = set()

def normalize_symbol(raw_symbol: str) -> str:
    """
    将 MT4 原始品种名归一化为标准名。
    MT4 经纪商经常给品种加后缀: EURUSDm, XAUUSD.a, NASUSDc, U30USD.r 等
    """
    if not raw_symbol:
        return ""
    s = raw_symbol.strip().upper()
    if s in KNOWN_SYMBOLS:
        return s
    # 去掉末尾 1~4 字符尝试匹配
    for trim in range(1, 5):
        candidate = s[:-trim] if len(s) > trim else ""
        if candidate and candidate in KNOWN_SYMBOLS:
            return candidate
    # 去掉点号后缀
    if '.' in s:
        candidate = s.split('.')[0]
        if candidate in KNOWN_SYMBOLS:
            return candidate
    if '_' in s:
        candidate = s.split('_')[0]
        if candidate in KNOWN_SYMBOLS:
            return candidate
    return s

def get_kline_lock(symbol):
    if symbol not in kline_locks:
        kline_locks[symbol] = threading.Lock()
    return kline_locks[symbol]

def _floor_ts(ts_ms, tf):
    if tf == "5min":     step = 5 * 60 * 1000
    elif tf == "10min":  step = 10 * 60 * 1000
    elif tf == "1hour":  step = 60 * 60 * 1000
    else:                step = 60 * 1000
    return (ts_ms // step) * step

def update_kline(symbol, bid, ask, tick_ts_ms):
    """bar 格式: [ts, open, high, low, close]  索引: 0, 1, 2, 3, 4"""
    mid = (bid + ask) / 2.0
    norm = normalize_symbol(symbol)
    tfs = ["5min", "10min", "1hour"]
    with get_kline_lock(norm):
        if norm not in kline_data:
            kline_data[norm] = {tf: [] for tf in tfs}
        for tf in tfs:
            bar_ts = _floor_ts(tick_ts_ms, tf)
            bars = kline_data[norm][tf]
            if bars and bars[-1][0] == bar_ts:
                bar = bars[-1]
                # ★ 修复: open(bar[1])不动, 只更新 high/low/close
                bar[2] = max(bar[2], mid)   # high
                bar[3] = min(bar[3], mid)   # low
                bar[4] = mid                # close
            else:
                bars.append([bar_ts, mid, mid, mid, mid])
                if len(bars) > KLINE_MAX[tf]:
                    bars[:] = bars[-KLINE_MAX[tf]:]

history_status = deque(maxlen=MAX_HISTORY)
history_positions = deque(maxlen=MAX_HISTORY)
history_report = deque(maxlen=MAX_HISTORY)
history_poll = deque(maxlen=MAX_HISTORY)
history_echo = deque(maxlen=MAX_HISTORY)
history_lock = threading.RLock()

# ==================== 产品规则表（完整版） ====================
PRODUCT_SPECS = {
    "USDCHF":{"size":100000,"lev":500,"currency":"CHF","type":"forex"},
    "GBPUSD":{"size":100000,"lev":500,"currency":"USD","type":"forex"},
    "EURUSD":{"size":100000,"lev":500,"currency":"USD","type":"forex"},
    "USDJPY":{"size":100000,"lev":500,"currency":"JPY","type":"forex"},
    "USDCAD":{"size":100000,"lev":500,"currency":"CAD","type":"forex"},
    "AUDUSD":{"size":100000,"lev":500,"currency":"USD","type":"forex"},
    "EURGBP":{"size":100000,"lev":500,"currency":"GBP","type":"forex"},
    "EURAUD":{"size":100000,"lev":500,"currency":"AUD","type":"forex"},
    "EURCHF":{"size":100000,"lev":500,"currency":"CHF","type":"forex"},
    "EURJPY":{"size":100000,"lev":500,"currency":"JPY","type":"forex"},
    "GBPCHF":{"size":100000,"lev":500,"currency":"CHF","type":"forex"},
    "CADJPY":{"size":100000,"lev":500,"currency":"JPY","type":"forex"},
    "GBPJPY":{"size":100000,"lev":500,"currency":"JPY","type":"forex"},
    "AUDNZD":{"size":100000,"lev":500,"currency":"NZD","type":"forex"},
    "AUDCAD":{"size":100000,"lev":500,"currency":"CAD","type":"forex"},
    "AUDCHF":{"size":100000,"lev":500,"currency":"CHF","type":"forex"},
    "AUDJPY":{"size":100000,"lev":500,"currency":"JPY","type":"forex"},
    "CHFJPY":{"size":100000,"lev":500,"currency":"JPY","type":"forex"},
    "EURNZD":{"size":100000,"lev":500,"currency":"NZD","type":"forex"},
    "EURCAD":{"size":100000,"lev":500,"currency":"CAD","type":"forex"},
    "CADCHF":{"size":100000,"lev":500,"currency":"CHF","type":"forex"},
    "NZDJPY":{"size":100000,"lev":500,"currency":"JPY","type":"forex"},
    "NZDUSD":{"size":100000,"lev":500,"currency":"USD","type":"forex"},
    "GBPAUD":{"size":100000,"lev":500,"currency":"AUD","type":"forex"},
    "GBPCAD":{"size":100000,"lev":500,"currency":"CAD","type":"forex"},
    "GBPNZD":{"size":100000,"lev":500,"currency":"NZD","type":"forex"},
    "NZDCAD":{"size":100000,"lev":500,"currency":"CAD","type":"forex"},
    "NZDCHF":{"size":100000,"lev":500,"currency":"CHF","type":"forex"},
    "USDSGD":{"size":100000,"lev":500,"currency":"SGD","type":"forex"},
    "USDHKD":{"size":100000,"lev":500,"currency":"HKD","type":"forex"},
    "USDCNH":{"size":100000,"lev":500,"currency":"CNH","type":"forex"},
    "U30USD":{"size":10,"lev":100,"currency":"USD","type":"index"},
    "NASUSD":{"size":10,"lev":100,"currency":"USD","type":"index"},
    "SPXUSD":{"size":100,"lev":100,"currency":"USD","type":"index"},
    "100GBP":{"size":10,"lev":100,"currency":"GBP","type":"index"},
    "D30EUR":{"size":10,"lev":100,"currency":"EUR","type":"index"},
    "E50EUR":{"size":10,"lev":100,"currency":"EUR","type":"index"},
    "H33HKD":{"size":100,"lev":100,"currency":"HKD","type":"index"},
    "UKOUSD":{"size":1000,"lev":500,"currency":"USD","type":"commodity"},
    "USOUSD":{"size":1000,"lev":500,"currency":"USD","type":"commodity"},
    "XAGUSD":{"size":5000,"lev":500,"currency":"USD","type":"metal"},
    "XAUUSD":{"size":100,"lev":500,"currency":"USD","type":"metal"},
    "BTCUSD":{"size":1,"lev":500,"currency":"USD","type":"crypto"},
    "BCHUSD":{"size":100,"lev":500,"currency":"USD","type":"crypto"},
    "RPLUSD":{"size":10000,"lev":500,"currency":"USD","type":"crypto"},
    "LTCUSD":{"size":100,"lev":500,"currency":"USD","type":"crypto"},
    "ETHUSD":{"size":10,"lev":500,"currency":"USD","type":"crypto"},
    "XMRUSD":{"size":100,"lev":500,"currency":"USD","type":"crypto"},
    "BNBUSD":{"size":100,"lev":500,"currency":"USD","type":"crypto"},
    "SOLUSD":{"size":100,"lev":500,"currency":"USD","type":"crypto"},
    "LNKUSD":{"size":1000,"lev":500,"currency":"USD","type":"crypto"},
    "XSIUSD":{"size":1000,"lev":500,"currency":"USD","type":"crypto"},
    "DOGUSD":{"size":100000,"lev":500,"currency":"USD","type":"crypto"},
    "ADAUSD":{"size":10000,"lev":500,"currency":"USD","type":"crypto"},
    "AVEUSD":{"size":100,"lev":500,"currency":"USD","type":"crypto"},
    "DSHUSD":{"size":1000,"lev":500,"currency":"USD","type":"crypto"},
    "AAPL":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "AMZN":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "BABA":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "GOOGL":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "META":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "MSFT":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "NFLX":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "NVDA":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "TSLA":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "ABBV":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "ABNB":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "ABT":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "ADBE":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "AMD":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "AVGO":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "C":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "CRM":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "DIS":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "GS":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "INTC":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "JNJ":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "MA":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "MCD":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "KO":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "MMM":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "NIO":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "PLTR":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "SHOP":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "TSM":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "V":{"size":100,"lev":10,"currency":"USD","type":"stock"},
}

KNOWN_SYMBOLS = set(PRODUCT_SPECS.keys())

# ==================== 工具函数 ====================
def norm_str(x):
    return "" if x is None else str(x).strip()

def get_client_ip():
    return request.headers.get('X-Real-Ip') or request.headers.get('X-Forwarded-For', request.remote_addr)

def try_parse_json(raw_body: str):
    cleaned = (raw_body or "").strip()
    if not cleaned:
        return None, None, None, None
    parsed_json = parse_error = parse_error_detail = remaining_data = None
    try:
        decoder = json.JSONDecoder()
        parsed_json, idx = decoder.raw_decode(cleaned)
        remaining = cleaned[idx:].strip()
        if remaining:
            remaining_data = remaining[:200]
    except json.JSONDecodeError as e:
        parse_error = str(e)
        parse_error_detail = traceback.format_exc()
    except Exception as e:
        parse_error = f"未知异常: {str(e)}"
        parse_error_detail = traceback.format_exc()
    return parsed_json, parse_error, parse_error_detail, remaining_data

def detect_category(path, parsed_json):
    if path.endswith("/web/api/mt4/status"):    return "status"
    if path.endswith("/web/api/mt4/positions"):  return "positions"
    if path.endswith("/web/api/mt4/report"):     return "report"
    if path.endswith("/web/api/echo"):           return "echo"
    if path.endswith("/web/api/mt4/commands"):   return "poll"
    return "other"

def _symbol_matches(record_symbol_raw, filter_symbol):
    """模糊匹配: EURUSDm -> EURUSD"""
    if not filter_symbol: return True
    if not record_symbol_raw: return False
    return normalize_symbol(record_symbol_raw) == filter_symbol

def store_mt4_data(raw_body, client_ip, headers_dict):
    parsed_json, parse_error, parse_error_detail, remaining_data = try_parse_json(raw_body)
    category = detect_category(request.path, parsed_json if isinstance(parsed_json, dict) else None)
    record = {
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip": client_ip, "method": request.method, "path": request.path,
        "category": category, "headers": headers_dict, "body_raw": raw_body,
        "parsed": parsed_json, "parse_error": parse_error,
        "parse_error_detail": parse_error_detail, "remaining_data": remaining_data,
    }
    if isinstance(parsed_json, dict):
        for k in ["account","server","balance","equity","floating_pnl","leverage_used","risk_flags","exposure_notional","positions"]:
            record[k] = parsed_json.get(k)
    with history_lock:
        {"status":history_status,"positions":history_positions,"report":history_report,
         "poll":history_poll,"echo":history_echo}.get(category, history_echo).appendleft(record)
    return parsed_json, record

# ==================== 调试接口 ====================
@app.route("/api/debug/symbols", methods=["GET"])
def api_debug_symbols():
    """查看 MT4 推送的原始品种名 vs 归一化后的名字，调试匹配问题"""
    raw_syms = {}
    with history_lock:
        for r in history_report:
            p = r.get("parsed")
            if isinstance(p, dict) and p.get("symbol"):
                raw = p["symbol"]
                raw_syms[raw] = normalize_symbol(raw)
    return jsonify({"raw_to_normalized": raw_syms, "known_count": len(KNOWN_SYMBOLS)})

# ==================== 最新状态接口 ====================
@app.route("/api/latest_status", methods=["GET"])
def api_latest_status():
    sf = request.args.get("symbol", "").upper()
    detail = latest_quote = None
    with history_lock:
        sr = history_status[0] if history_status else None
        pr = history_positions[0] if history_positions else None

        for record in history_report:
            p = record.get("parsed")
            if not isinstance(p, dict): continue
            if p.get("desc") == "QUOTE_DATA":
                rs = p.get("symbol", "")
                if sf and not _symbol_matches(rs, sf): continue
                try:
                    qd = json.loads(p.get("message", "{}"))
                    if isinstance(qd, dict) and "bid" in qd:
                        latest_quote = {"bid":qd["bid"],"ask":qd["ask"],
                            "symbol":normalize_symbol(rs) or rs,
                            "spread":p.get("spread"),"ts":p.get("ts"),
                            "received_at":record.get("received_at")}
                        break
                except: pass

        if not latest_quote:
            for record in history_report:
                p = record.get("parsed")
                if not isinstance(p, dict): continue
                rs = p.get("symbol", "")
                if sf and not _symbol_matches(rs, sf): continue
                if p.get("bid") is not None and p.get("ask") is not None:
                    latest_quote = {"bid":p["bid"],"ask":p["ask"],
                        "symbol":normalize_symbol(rs) or rs,
                        "spread":p.get("spread"),"ts":p.get("ts"),
                        "received_at":record.get("received_at")}
                    break

        if not latest_quote and sr:
            pd = sr.get("parsed", {})
            if sf:
                for pos in (pd.get("positions") or []):
                    if _symbol_matches(norm_str(pos.get("symbol","")), sf):
                        latest_quote = {"bid":None,"ask":None,"symbol":sf,
                            "spread":None,"ts":None,"received_at":sr.get("received_at")}
                        break

        if sr:
            pd = sr.get("parsed", {})
            detail = {k: pd.get(k) for k in ["account","server","balance","equity","margin","free_margin","margin_level","floating_pnl","leverage_used"]}
            detail["received_at"] = sr.get("received_at")
            detail["positions"] = pd.get("positions") or (pr.get("parsed",{}).get("positions") if pr else [])
        elif pr:
            pd = pr.get("parsed", {})
            detail = {"received_at":pr.get("received_at"),"account":pd.get("account"),"server":pd.get("server"),
                "positions":pd.get("positions") or []}

    return jsonify({"detail":detail,"latest_quote":latest_quote})

# ==================== MT4 数据接收 ====================
@app.route("/web/api/mt4/status", methods=["POST"])
def mt4_status():
    store_mt4_data(request.get_data(as_text=True), get_client_ip(), dict(request.headers))
    return "OK", 200

@app.route("/web/api/mt4/positions", methods=["POST"])
def mt4_positions():
    store_mt4_data(request.get_data(as_text=True), get_client_ip(), dict(request.headers))
    return "OK", 200

@app.route("/web/api/mt4/report", methods=["POST"])
def mt4_report():
    store_mt4_data(request.get_data(as_text=True), get_client_ip(), dict(request.headers))
    return "OK", 200

@app.route("/web/api/mt4/quote", methods=["POST"])
def mt4_quote():
    store_mt4_data(request.get_data(as_text=True), get_client_ip(), dict(request.headers))
    return "OK", 200

@app.route("/web/api/echo", methods=["POST"])
def mt4_webhook_echo():
    store_mt4_data(request.get_data(as_text=True), get_client_ip(), dict(request.headers))
    return "OK", 200

@app.route("/web/api/mt4/commands", methods=["POST"])
def mt4_commands():
    store_mt4_data(request.get_data(as_text=True), get_client_ip(), dict(request.headers))
    return jsonify({"commands": []}), 200

# ==================== Tick 推送 ====================
@app.route('/api/tick', methods=['POST'])
def receive_tick():
    try:
        ticks = request.json
        if not isinstance(ticks, list): ticks = [ticks]
        for tick in ticks:
            sym_raw = tick.get('symbol')
            bid, ask = tick.get('bid'), tick.get('ask')
            tt = tick.get('tick_time')
            if not sym_raw or not bid or not ask: continue
            ts_ms = (tt * 1000) if tt else int(time.time() * 1000)
            update_kline(sym_raw, float(bid), float(ask), ts_ms)
            record = {
                "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ip": get_client_ip(), "method": "POST", "path": "/api/tick",
                "category": "report", "headers": {}, "body_raw": json.dumps(tick),
                "parsed": {"desc":"QUOTE_DATA","spread":tick.get('spread',0),"ts":tt,
                    "message":json.dumps({"bid":bid,"ask":ask}),
                    "symbol":sym_raw,"account":"tick_stream"}
            }
            with history_lock:
                history_report.appendleft(record)
        return '', 204
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== K线 & 产品接口 ====================
@app.route("/api/kline", methods=["GET"])
def api_kline():
    symbol = request.args.get("symbol", "XAUUSD").upper()
    tf = request.args.get("tf", "5min")
    if tf not in KLINE_MAX: tf = "5min"
    limit = min(request.args.get("limit", KLINE_MAX[tf], type=int), KLINE_MAX[tf])
    with get_kline_lock(symbol):
        bars = list(kline_data.get(symbol, {}).get(tf, []))
    return jsonify({"symbol":symbol,"tf":tf,"bars":bars[-limit:]})

@app.route("/api/products", methods=["GET"])
def api_products():
    return jsonify(PRODUCT_SPECS)

# ==================== 前端页面 ====================
HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="theme-color" content="#0b0e11"/>
<title>量化交易终端</title>
<style>
:root{--bg:#0b0e11;--card:#161a1e;--card2:#1e2329;--text:#eaecef;--muted:#5e6673;
--line:#2b3139;--green:#0ecb81;--red:#f6465d;--yellow:#f0b90b;--accent:#f0b90b;
--safe-b:env(safe-area-inset-bottom)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;outline:none}
html{font-size:16px}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
color:var(--text);background:var(--bg);line-height:1.5;overflow-x:hidden}
.app{width:100%;max-width:72rem;margin:0 auto;min-height:100vh;padding:1rem;
padding-bottom:calc(1rem + var(--safe-b))}
.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:.75rem}
.logo{font-size:1.125rem;font-weight:700;color:var(--accent)}
.conn{display:flex;align-items:center;gap:.4rem;font-size:.8rem;font-weight:600;color:var(--muted)}
.dot{width:.5rem;height:.5rem;border-radius:50%;background:#555;transition:.3s}
.dot.ok{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.err{background:var(--red);box-shadow:0 0 6px var(--red)}
.tw{overflow-x:auto;white-space:nowrap;margin-bottom:.75rem;scrollbar-width:none;-webkit-overflow-scrolling:touch}
.tw::-webkit-scrollbar{display:none}
.tabs{display:inline-flex;gap:.375rem}
.tab{padding:.4rem .875rem;border-radius:.375rem;background:0;color:var(--muted);font-weight:600;
border:1px solid transparent;font-size:.8rem;cursor:pointer;transition:.2s}
.tab:hover{color:var(--text)}.tab.on{background:var(--card2);color:var(--accent);border-color:var(--line)}
.sr{display:flex;align-items:center;justify-content:space-between;margin-bottom:.75rem;
background:var(--card);padding:.875rem 1.25rem;border-radius:.75rem;border:1px solid var(--line)}
.clk{font-family:"SF Mono",Menlo,monospace;font-weight:700;color:var(--muted);font-size:1.25rem}
.sc{display:flex;align-items:center;gap:.75rem}
.sn{font-size:1.375rem;font-weight:800;letter-spacing:.5px}
.sb{font-size:.75rem;padding:.25rem .625rem;border-radius:.25rem;background:var(--card2);
color:var(--muted);font-weight:600;border:1px solid var(--line);cursor:pointer;transition:.15s}
.sb:hover{color:var(--accent);border-color:var(--accent)}
.ba{display:flex;align-items:center;gap:.75rem;font-family:"SF Mono",monospace;font-weight:700;font-size:1rem}
.ba-b{color:var(--green)}.ba-a{color:var(--red)}.ba-s{color:var(--muted);font-size:.75rem}
.main{display:grid;grid-template-columns:280px 1fr;gap:.75rem;margin-bottom:.75rem}
.pc{background:var(--card);border:1px solid var(--line);border-radius:.75rem;padding:1.25rem;
display:flex;flex-direction:column;gap:1rem}
.pm{text-align:center}
.pv{font-size:2.25rem;font-weight:800;letter-spacing:-.5px;font-family:"SF Mono",monospace;transition:color .2s}
.pv.up{color:var(--green)}.pv.down{color:var(--red)}
.pr{display:flex;justify-content:space-between;align-items:center;padding:.4rem 0;border-bottom:1px solid var(--line)}
.pr:last-child{border-bottom:0}
.pl{font-size:.8rem;font-weight:600;color:var(--muted)}
.pvv{font-size:1rem;font-weight:700;font-family:"SF Mono",monospace}
.pvv.bid{color:var(--green)}.pvv.ask{color:var(--red)}
.cp{background:var(--card);border:1px solid var(--line);border-radius:.75rem;display:flex;
flex-direction:column;overflow:hidden;min-height:380px}
.ch{display:flex;align-items:center;justify-content:space-between;padding:.625rem 1rem;
border-bottom:1px solid var(--line);flex-shrink:0}
.ct{font-size:.8rem;font-weight:700;color:var(--muted)}
.ci{font-size:.75rem;color:var(--muted);font-family:monospace}
.tft{display:flex;gap:.25rem}
.tf{padding:.2rem .625rem;border-radius:.25rem;border:1px solid var(--line);background:0;
color:var(--muted);font-weight:600;font-size:.75rem;cursor:pointer;transition:.15s}
.tf:hover{color:var(--text)}.tf.on{background:var(--accent);color:#000;border-color:var(--accent)}
.cw{flex:1;position:relative;min-height:0;cursor:crosshair}
#kc,#cc{position:absolute;inset:0;width:100%;height:100%}#cc{pointer-events:none}
.stb{background:var(--card);border:1px solid var(--line);border-radius:.75rem;padding:.625rem 1rem;
display:flex;justify-content:space-between;align-items:center}
.si{display:flex;align-items:center;gap:.375rem;font-size:.75rem;font-weight:600;color:var(--muted)}
.sg{width:.5rem;height:.5rem;border-radius:50%;display:inline-block}
.sg.g{background:var(--green);box-shadow:0 0 4px var(--green)}
.sg.y{background:var(--yellow);box-shadow:0 0 4px var(--yellow)}
.sg.r{background:var(--red);box-shadow:0 0 4px var(--red)}
.mask{position:fixed;inset:0;background:rgba(0,0,0,.65);display:none;align-items:center;
justify-content:center;padding:1rem;z-index:100;backdrop-filter:blur(4px)}
.mdl{width:min(24rem,100%);background:var(--card);border-radius:1rem;overflow:hidden;
border:1px solid var(--line);box-shadow:0 20px 40px rgba(0,0,0,.4);animation:pop .2s ease-out;
display:flex;flex-direction:column;max-height:80vh}
@keyframes pop{from{transform:scale(.95);opacity:0}to{transform:scale(1);opacity:1}}
.mh{padding:1rem 1.25rem;display:flex;justify-content:space-between;align-items:center;
border-bottom:1px solid var(--line);font-weight:700;font-size:1rem;flex-shrink:0}
.mb{padding:.5rem;overflow-y:auto;flex-grow:1}
.it{padding:.75rem 1rem;font-weight:600;display:flex;justify-content:space-between;
align-items:center;border-radius:.5rem;cursor:pointer;transition:.1s;color:var(--muted)}
.it:hover{background:var(--card2);color:var(--text)}.it.on{color:var(--accent)}
.xb{background:0;border:0;color:var(--muted);font-size:1.25rem;cursor:pointer}
@media(max-width:768px){
.main{grid-template-columns:1fr}.cp{min-height:320px}
.sr{flex-wrap:wrap;gap:.5rem}
.mask{align-items:flex-end;padding:0}
.mdl{width:100%;border-radius:1rem 1rem 0 0;animation:sU .25s ease-out;padding-bottom:var(--safe-b)}
@keyframes sU{from{transform:translateY(100%)}to{transform:translateY(0)}}
}
</style>
</head>
<body>
<div class="app">
<div class="topbar"><div class="logo">MT4 量化终端</div>
<div class="conn"><span id="cT">等待连接...</span><span class="dot" id="cD"></span></div></div>

<div class="tw"><div class="tabs">
<button class="tab on" onclick="stab(this,'forex')">外汇</button>
<button class="tab" onclick="stab(this,'metal')">贵金属</button>
<button class="tab" onclick="stab(this,'commodity')">大宗商品</button>
<button class="tab" onclick="stab(this,'index')">指数</button>
<button class="tab" onclick="stab(this,'crypto')">虚拟货币</button>
<button class="tab" onclick="stab(this,'stock')">股票</button>
</div></div>

<div class="sr">
<div class="clk" id="clk">--:--:--</div>
<div class="sc"><span class="sn" id="sN">EURUSD</span><span class="sb" onclick="openM()">切换 ▾</span></div>
<div class="ba"><span class="ba-b" id="bB">--</span><span class="ba-s">/</span><span class="ba-a" id="bA">--</span></div>
</div>

<div class="main">
<div class="pc">
<div class="pm"><span class="pv" id="mP">--</span></div>
<div>
<div class="pr"><span class="pl">Bid（买入）</span><span class="pvv bid" id="bP">--</span></div>
<div class="pr"><span class="pl">Mid（中间价）</span><span class="pvv" id="mP2">--</span></div>
<div class="pr"><span class="pl">Ask（卖出）</span><span class="pvv ask" id="aP">--</span></div>
<div class="pr"><span class="pl">点差</span><span class="pvv" id="sV" style="color:var(--accent)">--</span></div>
</div>
</div>

<div class="cp">
<div class="ch">
<div style="display:flex;align-items:center;gap:.75rem"><div class="ct">K 线图</div><div class="ci" id="oI"></div></div>
<div class="tft">
<button class="tf on" data-tf="5min" onclick="stf(this)">5m</button>
<button class="tf" data-tf="10min" onclick="stf(this)">10m</button>
<button class="tf" data-tf="1hour" onclick="stf(this)">1H</button>
</div>
</div>
<div class="cw" id="cW"><canvas id="kc"></canvas><canvas id="cc"></canvas></div>
</div>
</div>

<div class="stb">
<div class="si"><span class="sg g" id="lS"></span><span id="lT">延迟: --</span></div>
<div class="si"><span id="sT">点差: --</span></div>
<div class="si"><span id="uT">数据: --</span></div>
</div>
</div>

<div class="mask" id="msk" onclick="if(event.target.id==='msk')msk.style.display='none'">
<div class="mdl"><div class="mh"><span id="mTi">切换品种</span><button class="xb" onclick="msk.style.display='none'">✕</button></div>
<div class="mb" id="mLi"></div></div>
</div>

<script>
const $=id=>document.getElementById(id);
const CP={
forex:["EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURGBP","EURJPY","GBPJPY","AUDJPY","EURAUD","EURCHF","GBPCHF","CHFJPY","CADJPY","AUDNZD","AUDCAD","AUDCHF","EURNZD","EURCAD","CADCHF","NZDJPY","GBPAUD","GBPCAD","GBPNZD","NZDCAD","NZDCHF","USDSGD","USDHKD","USDCNH"],
metal:["XAUUSD","XAGUSD"],commodity:["UKOUSD","USOUSD"],
index:["U30USD","NASUSD","SPXUSD","100GBP","D30EUR","E50EUR","H33HKD"],
crypto:["BTCUSD","ETHUSD","LTCUSD","BCHUSD","XMRUSD","BNBUSD","SOLUSD","ADAUSD","DOGUSD","XSIUSD","AVEUSD","DSHUSD","RPLUSD","LNKUSD"],
stock:["AAPL","AMZN","BABA","GOOGL","META","MSFT","NFLX","NVDA","TSLA","ABBV","ABNB","ABT","ADBE","AMD","AVGO","C","CRM","DIS","GS","INTC","JNJ","MA","MCD","KO","MMM","NIO","PLTR","SHOP","TSM","V"]
};
const CN={forex:"外汇",metal:"贵金属",commodity:"大宗商品",index:"指数",crypto:"虚拟货币",stock:"股票"};
let S="EURUSD",C="forex",TF="5min",lastM=null,stag=0,sigP=0,cBars=[],L=null;

function dg(s){if(!s)return 2;const u=s.toUpperCase();
if(u==="XAUUSD"||u==="XAGUSD"||u==="UKOUSD"||u==="USOUSD")return 2;
if(u==="BTCUSD"||u==="ETHUSD")return 2;
if(u.endsWith("JPY"))return 3;
if(["DOGUSD","ADAUSD","RPLUSD","XSIUSD","LNKUSD"].includes(u))return 5;
if(["LTCUSD","SOLUSD","BCHUSD","XMRUSD","BNBUSD","AVEUSD","DSHUSD"].includes(u))return 2;
if(["U30USD","NASUSD","SPXUSD","100GBP","D30EUR","E50EUR","H33HKD"].includes(u))return 1;
// 股票: 没有典型外汇后缀
const fx=["USD","GBP","EUR","JPY","CHF","AUD","NZD","CAD","HKD","SGD","CNH"];
if(!fx.some(c=>u.includes(c)))return 2;
return 5;}
function fm(n,d){return n!=null?parseFloat(n).toFixed(d):'--'}

function nS(lo,hi,mt){
if(lo===hi){const d=lo===0?1:Math.abs(lo)*.01;lo-=d;hi+=d}
const r=hi-lo,p=r*.08;let mn=lo-p,mx=hi+p;
const rs=(mx-mn)/(mt-1),mg=Math.pow(10,Math.floor(Math.log10(rs))),res=rs/mg;
let ns;if(res<=1)ns=mg;else if(res<=2)ns=2*mg;else if(res<=2.5)ns=2.5*mg;
else if(res<=5)ns=5*mg;else ns=10*mg;
const tI=Math.floor(mn/ns)*ns,tA=Math.ceil(mx/ns)*ns,tk=[];
for(let v=tI;v<=tA+ns*.5;v+=ns)tk.push(v);
return{tI,tA,ns,tk}}

function tick(){const n=new Date();$('clk').innerText=[n.getHours(),n.getMinutes(),n.getSeconds()].map(v=>String(v).padStart(2,'0')).join(':')}
function stab(b,c){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));b.classList.add('on');C=c}
function openM(){$('mTi').innerText='切换'+(CN[C]||'品种');
$('mLi').innerHTML=(CP[C]||[]).map(s=>`<div class="it ${s===S?'on':''}" onclick="pick('${s}')"><span>${s}</span></div>`).join('');
$('msk').style.display='flex'}
function pick(s){S=s;$('sN').innerText=s;$('msk').style.display='none';
['bP','aP','mP','mP2','sV','bB','bA'].forEach(id=>{if($(id))$(id).innerText='--'});
$('sT').innerText='点差: --';lastM=null;stag=0;drawE();rf();fK()}

async function rf(){try{
const r=await fetch('/api/latest_status?symbol='+S),d=await r.json();
cOk(true);
if(d.latest_quote){const q=d.latest_quote,D=dg(S);
if(q.bid!=null&&q.ask!=null){
const mid=(q.bid+q.ask)/2,prev=lastM;lastM=mid;
$('bP').innerText=fm(q.bid,D);$('aP').innerText=fm(q.ask,D);
$('mP').innerText=fm(mid,D);$('mP2').innerText=fm(mid,D);
$('bB').innerText=fm(q.bid,D);$('bA').innerText=fm(q.ask,D);
const pe=$('mP');pe.classList.remove('up','down');
if(prev!=null){if(mid>prev)pe.classList.add('up');else if(mid<prev)pe.classList.add('down')}
const sp=q.spread!=null?q.spread:((q.ask-q.bid)*Math.pow(10,D>2?0:(5-D))).toFixed(1);
$('sV').innerText=sp;$('sT').innerText='点差: '+sp}
const now=Date.now();
if(q.ts!=null){const sMs=q.ts<1e12?q.ts*1000:q.ts,lat=Math.round(now-sMs-8*3600000);
$('lT').innerText='延迟: '+lat+'ms';lSig(lat);
const dt=new Date(sMs+8*3600000);
$('uT').innerText='数据: '+[dt.getUTCMonth()+1,dt.getUTCDate()].map(v=>String(v).padStart(2,'0')).join('-')+' '+
[dt.getUTCHours(),dt.getUTCMinutes(),dt.getUTCSeconds()].map(v=>String(v).padStart(2,'0')).join(':')}
else if(q.received_at)$('uT').innerText='更新: '+(q.received_at.split(' ')[1]||'--')
}}catch(e){console.error(e);cOk(false)}}

async function fK(){try{
const lm={_5min:300,_10min:250,_1hour:200};
const r=await fetch('/api/kline?symbol='+S+'&tf='+TF+'&limit='+(lm['_'+TF]||300)),d=await r.json();
if(d.bars&&d.bars.length>0){cBars=d.bars;drawK(d.bars)}else{cBars=[];drawE()}
}catch(e){drawE()}}
function stf(b){document.querySelectorAll('.tf').forEach(t=>t.classList.remove('on'));b.classList.add('on');TF=b.dataset.tf;fK()}

// ========== K线绘制 ==========
function drawK(bars){
const cv=$('kc');if(!cv)return;const ctx=cv.getContext('2d');
const dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
if(w<=0||h<=0)return;cv.width=w*dpr;cv.height=h*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
if(!bars||!bars.length){drawE();return}
const PL=8,PR=72,PT=16,PB=24,cW=w-PL-PR,cH=h-PT-PB;
if(cW<=0||cH<=0)return;
const df={_5min:80,_10min:60,_1hour:48};
let vn=Math.min(df['_'+TF]||80,bars.length,Math.floor(cW/6));
vn=Math.max(vn,Math.min(20,bars.length));
const vb=bars.slice(-vn),n=vb.length;if(!n)return;
let dMin=Infinity,dMax=-Infinity;
for(const b of vb){if(b[2]>dMax)dMax=b[2];if(b[3]<dMin)dMin=b[3]}
const D=dg(S),sc=nS(dMin,dMax,6),yI=sc.tI,yA=sc.tA,yR=yA-yI||1;
const p2y=p=>PT+cH*(1-(p-yI)/yR),y2p=y=>yI+(1-(y-PT)/cH)*yR;
const bT=cW/n,gap=Math.max(1,Math.round(bT*.2)),bW=Math.max(1,Math.floor(bT-gap)),hB=bW/2;
const bx=i=>PL+(i+.5)*bT;
L={PL,PR,PT,PB,cW,cH,n,bT,yI,yA,yR,p2y,y2p,bx,D,vb};
const G='#0ecb81',R='#f6465d',gr='rgba(255,255,255,0.04)',ax='#5e6673';
// Y轴
ctx.font='11px "SF Mono",Menlo,monospace';ctx.textAlign='left';ctx.textBaseline='middle';
for(const t of sc.tk){const y=p2y(t);if(y<PT-2||y>PT+cH+2)continue;
ctx.strokeStyle=gr;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(PL,Math.round(y)+.5);ctx.lineTo(PL+cW,Math.round(y)+.5);ctx.stroke();
ctx.fillStyle=ax;ctx.fillText(t.toFixed(D),PL+cW+8,y)}
// 烛台
for(let i=0;i<n;i++){const b=vb[i],o=b[1],hi=b[2],lo=b[3],cl=b[4];
const up=cl>=o,col=up?G:R,x=bx(i);
ctx.strokeStyle=col;ctx.lineWidth=1;ctx.beginPath();
ctx.moveTo(Math.round(x)+.5,Math.round(p2y(hi)));ctx.lineTo(Math.round(x)+.5,Math.round(p2y(lo)));ctx.stroke();
const yO=p2y(o),yC=p2y(cl),bt=Math.min(yO,yC),bb=Math.max(yO,yC);
ctx.fillStyle=col;ctx.fillRect(Math.round(x-hB),Math.round(bt),bW,Math.max(1,bb-bt))}
// 最新价虚线
if(n>0){const lc=vb[n-1][4],up=lc>=vb[n-1][1],col=up?G:R,yL=p2y(lc);
ctx.setLineDash([4,3]);ctx.strokeStyle=col;ctx.lineWidth=1;
ctx.beginPath();ctx.moveTo(PL,Math.round(yL)+.5);ctx.lineTo(PL+cW,Math.round(yL)+.5);ctx.stroke();ctx.setLineDash([]);
const tW=PR-6,tH=18,tX=PL+cW+2,tY=Math.round(yL)-tH/2;
ctx.fillStyle=col;ctx.beginPath();ctx.roundRect(tX,tY,tW,tH,3);ctx.fill();
ctx.fillStyle='#fff';ctx.font='bold 11px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText(lc.toFixed(D),tX+tW/2,Math.round(yL))}
// X轴
ctx.fillStyle=ax;ctx.font='10px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='top';
const mG=60,iMs=TF==='1hour'?3600000*4:TF==='10min'?3600000:1800000;let lX=-Infinity;
for(let i=0;i<n;i++){const ts=vb[i][0],x=bx(i);
if(ts%iMs!==0&&i!==0&&i!==n-1)continue;if(x-lX<mG)continue;
const d=new Date(ts+8*3600000);let lb;
if(TF==='1hour')lb=(d.getUTCMonth()+1)+'/'+d.getUTCDate()+' '+String(d.getUTCHours()).padStart(2,'0')+':00';
else lb=String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0');
ctx.strokeStyle=gr;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(Math.round(x)+.5,PT+cH);ctx.lineTo(Math.round(x)+.5,PT+cH+4);ctx.stroke();
ctx.fillStyle=ax;ctx.fillText(lb,x,PT+cH+6);lX=x}
clrCH()}

function drawE(){const cv=$('kc');if(!cv)return;const ctx=cv.getContext('2d'),dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
cv.width=w*dpr;cv.height=h*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
ctx.fillStyle='#5e6673';ctx.font='13px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText('暂无 K 线数据',w/2,h/2);clrCH();L=null}

// ===== 十字光标 =====
function clrCH(){const cv=$('cc');if(!cv)return;const dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
cv.width=w*dpr;cv.height=h*dpr;cv.getContext('2d').scale(dpr,dpr);$('oI').innerText=''}
function dCH(mx,my){const cv=$('cc');if(!cv||!L)return;
const dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
cv.width=w*dpr;cv.height=h*dpr;const ctx=cv.getContext('2d');ctx.scale(dpr,dpr);
const{PL,PT,cW,cH,n,bT,D,vb,p2y,y2p,bx}=L;
const cx=Math.max(PL,Math.min(mx,PL+cW)),cy=Math.max(PT,Math.min(my,PT+cH));
const idx=Math.max(0,Math.min(Math.round((cx-PL)/bT-.5),n-1)),sx=bx(idx);
ctx.setLineDash([3,3]);ctx.strokeStyle='rgba(255,255,255,0.25)';ctx.lineWidth=1;
ctx.beginPath();ctx.moveTo(Math.round(sx)+.5,PT);ctx.lineTo(Math.round(sx)+.5,PT+cH);ctx.stroke();
ctx.beginPath();ctx.moveTo(PL,Math.round(cy)+.5);ctx.lineTo(PL+cW,Math.round(cy)+.5);ctx.stroke();
ctx.setLineDash([]);
const pr=y2p(cy);ctx.fillStyle='#2b3139';
const tW2=64,tH2=18,tX2=PL+cW+2,tY2=Math.round(cy)-tH2/2;
ctx.beginPath();ctx.roundRect(tX2,tY2,tW2,tH2,3);ctx.fill();
ctx.fillStyle='#eaecef';ctx.font='11px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText(pr.toFixed(D),tX2+tW2/2,Math.round(cy));
if(idx>=0&&idx<n){const b=vb[idx],ts=b[0],d=new Date(ts+8*3600000);
let lb;if(TF==='1hour')lb=(d.getUTCMonth()+1)+'/'+d.getUTCDate()+' '+String(d.getUTCHours()).padStart(2,'0')+':00';
else lb=String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0');
const tw=ctx.measureText(lb).width+12,tx=Math.round(sx)-tw/2,ty=PT+cH+2;
ctx.fillStyle='#2b3139';ctx.beginPath();ctx.roundRect(tx,ty,tw,16,3);ctx.fill();
ctx.fillStyle='#eaecef';ctx.font='10px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='top';
ctx.fillText(lb,Math.round(sx),ty+2);
const o=b[1],hi=b[2],lo=b[3],cl=b[4],up=cl>=o,c2=up?'#0ecb81':'#f6465d';
$('oI').innerHTML=`<span style="color:${c2}">O:${o.toFixed(D)} H:${hi.toFixed(D)} L:${lo.toFixed(D)} C:${cl.toFixed(D)}</span>`}}

(function(){const wr=$('cW');if(!wr)return;
function pos(e){const r=wr.getBoundingClientRect();return e.touches?{x:e.touches[0].clientX-r.left,y:e.touches[0].clientY-r.top}:{x:e.clientX-r.left,y:e.clientY-r.top}}
wr.addEventListener('mousemove',e=>{const p=pos(e);dCH(p.x,p.y)});
wr.addEventListener('mouseleave',()=>clrCH());
wr.addEventListener('touchmove',e=>{e.preventDefault();const p=pos(e);dCH(p.x,p.y)},{passive:false});
wr.addEventListener('touchend',()=>clrCH())})();

function lSig(v){const s=$('lS');if(!s)return;s.className='sg';
if(v<500)s.classList.add('g');else if(v<2000)s.classList.add('y');else s.classList.add('r')}
function cOk(ok){const d=$('cD'),t=$('cT');if(!d||!t)return;d.className='dot';
if(ok){d.classList.add('ok');t.innerText='已连接'}else{d.classList.add('err');t.innerText='连接断开'}}
function chkS(){const p=parseFloat($('mP')?.innerText)||0;
if(sigP===p&&p>0){stag++;if(stag>=5){const s=$('lS');if(s){s.className='sg';s.classList.add('r')}$('lT').innerText='数据停滞!'}}
else stag=0;sigP=p}

tick();setInterval(tick,1000);rf();setInterval(rf,2000);setInterval(chkS,2000);
fK();setInterval(fK,3000);
addEventListener('resize',()=>{clearTimeout(window._rt);window._rt=setTimeout(()=>{if(cBars.length)drawK(cBars);else drawE()},100)});
</script>
</body></html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
