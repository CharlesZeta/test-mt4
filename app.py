import os
import json
import threading
import traceback
import time
import re
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, request, render_template_string, jsonify

# ==================== MySQL ====================
import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB

app = Flask(__name__)

# ==================== MySQL 连接池 ====================
MYSQL_HOST = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
MYSQL_USER = os.environ.get("MYSQL_USER", "root")
MYSQL_PASS = os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DB   = os.environ.get("MYSQL_DATABASE", "mt4data")

_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=10,
            mincached=2,
            maxcached=5,
            blocking=True,
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            passwd=MYSQL_PASS,
            db=MYSQL_DB,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
        )
    return _pool

def get_conn():
    return get_pool().connection()

def init_db():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tick_logs (
                    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
                    symbol      VARCHAR(20)  NOT NULL,
                    bid         DOUBLE       NOT NULL,
                    ask         DOUBLE       NOT NULL,
                    spread      DOUBLE       DEFAULT 0,
                    mid         DOUBLE       DEFAULT 0,
                    tick_time   BIGINT       NOT NULL COMMENT 'ms timestamp',
                    received_at DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                    INDEX idx_sym_time (symbol, tick_time),
                    INDEX idx_time (tick_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.close()
        print("[DB] tick_logs ready")
    except Exception as e:
        print(f"[DB] init failed (will retry): {e}")

# ==================== 写入缓冲区 ====================
FLUSH_SIZE = 50
FLUSH_INTERVAL = 2.0
_write_buf = []
_buf_lock = threading.Lock()
_last_flush = time.time()

def _buf_append(row):
    global _last_flush
    with _buf_lock:
        _write_buf.append(row)
        if len(_write_buf) >= FLUSH_SIZE:
            _flush_buf()

def _flush_buf():
    global _write_buf, _last_flush
    if not _write_buf:
        return
    batch = _write_buf[:]
    _write_buf = []
    _last_flush = time.time()
    threading.Thread(target=_do_insert, args=(batch,), daemon=True).start()

def _do_insert(batch):
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO tick_logs (symbol,bid,ask,spread,mid,tick_time,received_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                batch
            )
        conn.close()
    except Exception as e:
        print(f"[DB] batch insert failed ({len(batch)}): {e}")

def _flush_timer():
    while True:
        time.sleep(FLUSH_INTERVAL)
        with _buf_lock:
            if _write_buf and (time.time() - _last_flush) >= FLUSH_INTERVAL:
                _flush_buf()

threading.Thread(target=_flush_timer, daemon=True).start()

# ==================== 全局数据结构 ====================
MAX_HISTORY = 50
KLINE_MAX = {"5min": 300, "10min": 250, "1hour": 200}
kline_data = {}
kline_locks = {}
latest_quotes = {}
quotes_lock = threading.Lock()

# ==================== 品种名归一化 ====================
KNOWN_SYMBOLS = set()

def normalize_symbol(raw_symbol):
    if not raw_symbol:
        return ""
    s = raw_symbol.strip().upper()
    if s in KNOWN_SYMBOLS:
        return s
    for trim in range(1, 5):
        candidate = s[:-trim] if len(s) > trim else ""
        if candidate and candidate in KNOWN_SYMBOLS:
            return candidate
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
    if tf == "5min":
        step = 5 * 60 * 1000
    elif tf == "10min":
        step = 10 * 60 * 1000
    elif tf == "1hour":
        step = 60 * 60 * 1000
    else:
        step = 60 * 1000
    return (ts_ms // step) * step

def update_kline(symbol, bid, ask, tick_ts_ms):
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
                bar[2] = max(bar[2], mid)
                bar[3] = min(bar[3], mid)
                bar[4] = mid
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

# ==================== 产品规则表 ====================
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

def try_parse_json(raw_body):
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
        parse_error = str(e)
        parse_error_detail = traceback.format_exc()
    return parsed_json, parse_error, parse_error_detail, remaining_data

def detect_category(path, parsed_json):
    if path.endswith("/web/api/mt4/status"):
        return "status"
    if path.endswith("/web/api/mt4/positions"):
        return "positions"
    if path.endswith("/web/api/mt4/report"):
        return "report"
    if path.endswith("/web/api/echo"):
        return "echo"
    if path.endswith("/web/api/mt4/commands"):
        return "poll"
    return "other"

def _symbol_matches(record_symbol_raw, filter_symbol):
    if not filter_symbol:
        return True
    if not record_symbol_raw:
        return False
    return normalize_symbol(record_symbol_raw) == filter_symbol

def _update_quote_cache(symbol_raw, bid, ask, spread=None, ts=None):
    norm = normalize_symbol(symbol_raw)
    if not norm:
        return
    try:
        b = float(bid) if bid is not None else None
        a = float(ask) if ask is not None else None
    except (ValueError, TypeError):
        return
    if b is None or a is None:
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with quotes_lock:
        latest_quotes[norm] = {"bid": b, "ask": a, "spread": spread, "ts": ts, "received_at": now_str}

def store_mt4_data(raw_body, client_ip, headers_dict):
    parsed_json, parse_error, parse_error_detail, remaining_data = try_parse_json(raw_body)
    category = detect_category(request.path, parsed_json if isinstance(parsed_json, dict) else None)
    record = {
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip": client_ip,
        "method": request.method,
        "path": request.path,
        "category": category,
        "headers": headers_dict,
        "body_raw": raw_body,
        "parsed": parsed_json,
        "parse_error": parse_error,
        "parse_error_detail": parse_error_detail,
        "remaining_data": remaining_data,
    }
    if isinstance(parsed_json, dict):
        for k in ["account", "server", "balance", "equity", "floating_pnl", "leverage_used", "risk_flags", "exposure_notional", "positions"]:
            record[k] = parsed_json.get(k)
        sym = parsed_json.get("symbol", "")
        if sym:
            if parsed_json.get("desc") == "QUOTE_DATA":
                try:
                    qd = json.loads(parsed_json.get("message", "{}"))
                    if isinstance(qd, dict) and "bid" in qd:
                        _update_quote_cache(sym, qd["bid"], qd["ask"], spread=parsed_json.get("spread"), ts=parsed_json.get("ts"))
                except Exception:
                    pass
            elif parsed_json.get("bid") is not None and parsed_json.get("ask") is not None:
                _update_quote_cache(sym, parsed_json["bid"], parsed_json["ask"], spread=parsed_json.get("spread"), ts=parsed_json.get("ts"))
    with history_lock:
        target = {"status": history_status, "positions": history_positions, "report": history_report, "poll": history_poll, "echo": history_echo}.get(category, history_echo)
        target.appendleft(record)
    return parsed_json, record


# ================================================================
#  tick-ren + tick-hist
# ================================================================
def _process_tick(tick):
    sym_raw = tick.get('symbol')
    bid = tick.get('bid')
    ask = tick.get('ask')
    tt = tick.get('tick_time')
    if not sym_raw or bid is None or ask is None:
        return None
    bid_f = float(bid)
    ask_f = float(ask)
    spread = float(tick.get('spread', 0))
    ts_ms = int(tt * 1000) if tt else int(time.time() * 1000)
    mid = (bid_f + ask_f) / 2.0
    norm = normalize_symbol(sym_raw)
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{now_dt.microsecond // 1000:03d}"

    # 1) memory
    update_kline(sym_raw, bid_f, ask_f, ts_ms)
    _update_quote_cache(sym_raw, bid_f, ask_f, spread=spread, ts=tt)

    # 2) history_report for frontend compat
    record = {
        "received_at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "ip": get_client_ip(),
        "method": "POST",
        "path": request.path,
        "category": "report",
        "headers": {},
        "body_raw": json.dumps(tick),
        "parsed": {
            "desc": "QUOTE_DATA",
            "spread": spread,
            "ts": tt,
            "message": json.dumps({"bid": bid_f, "ask": ask_f}),
            "symbol": sym_raw,
            "account": "tick_stream",
        },
    }
    with history_lock:
        history_report.appendleft(record)

    # 3) MySQL buffer
    _buf_append((norm, bid_f, ask_f, spread, mid, ts_ms, now_str))
    return norm


@app.route('/api/tick-ren', methods=['POST'])
def tick_ren():
    try:
        ticks = request.json
        if not isinstance(ticks, list):
            ticks = [ticks]
        count = 0
        for t in ticks:
            if _process_tick(t):
                count += 1
        return jsonify({"ok": True, "processed": count}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/tick-hist', methods=['POST'])
def tick_hist():
    try:
        ticks = request.json
        if not isinstance(ticks, list):
            ticks = [ticks]
        rows = []
        now_dt = datetime.now()
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{now_dt.microsecond // 1000:03d}"
        for tick in ticks:
            sym_raw = tick.get('symbol')
            bid = tick.get('bid')
            ask = tick.get('ask')
            tt = tick.get('tick_time')
            if not sym_raw or bid is None or ask is None:
                continue
            bid_f = float(bid)
            ask_f = float(ask)
            spread = float(tick.get('spread', 0))
            ts_ms = int(tt * 1000) if tt else int(time.time() * 1000)
            mid = (bid_f + ask_f) / 2.0
            norm = normalize_symbol(sym_raw)
            rows.append((norm, bid_f, ask_f, spread, mid, ts_ms, now_str))
        if rows:
            try:
                conn = get_conn()
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO tick_logs (symbol,bid,ask,spread,mid,tick_time,received_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        rows,
                    )
                conn.close()
            except Exception as e:
                return jsonify({"ok": False, "error": f"DB: {e}", "attempted": len(rows)}), 500
        return jsonify({"ok": True, "inserted": len(rows)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ==================== 历史查询 ====================
def _parse_ts(val):
    if not val:
        return None
    val = val.strip()
    if val.isdigit():
        v = int(val)
        return v if v > 1e12 else v * 1000
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(val, fmt)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


@app.route('/api/hist', methods=['GET'])
def api_hist():
    symbol = request.args.get("symbol", "").upper()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    norm = normalize_symbol(symbol)
    limit = min(int(request.args.get("limit", 1000)), 10000)
    agg = request.args.get("agg", "raw")
    from_ts = _parse_ts(request.args.get("from"))
    to_ts = _parse_ts(request.args.get("to"))
    if from_ts is None:
        from_ts = int(time.time() * 1000) - 3600 * 1000
    if to_ts is None:
        to_ts = int(time.time() * 1000) + 60000
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            if agg == "raw":
                cur.execute(
                    "SELECT symbol,bid,ask,spread,mid,tick_time,received_at FROM tick_logs "
                    "WHERE symbol=%s AND tick_time>=%s AND tick_time<=%s ORDER BY tick_time ASC LIMIT %s",
                    (norm, from_ts, to_ts, limit),
                )
                rows = cur.fetchall()
                for r in rows:
                    if isinstance(r.get("received_at"), datetime):
                        r["received_at"] = r["received_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            elif agg in ("1s", "5s", "1min"):
                step = {"1s": 1000, "5s": 5000, "1min": 60000}[agg]
                cur.execute(
                    "SELECT FLOOR(tick_time/%s)*%s AS ts, "
                    "SUBSTRING_INDEX(GROUP_CONCAT(mid ORDER BY tick_time ASC),',',1)+0 AS open, "
                    "MAX(mid) AS high, MIN(mid) AS low, "
                    "SUBSTRING_INDEX(GROUP_CONCAT(mid ORDER BY tick_time DESC),',',1)+0 AS close, "
                    "COUNT(*) AS ticks, AVG(spread) AS avg_spread "
                    "FROM tick_logs WHERE symbol=%s AND tick_time>=%s AND tick_time<=%s "
                    "GROUP BY FLOOR(tick_time/%s) ORDER BY ts ASC LIMIT %s",
                    (step, step, norm, from_ts, to_ts, step, limit),
                )
                rows = cur.fetchall()
                for r in rows:
                    r["ts"] = int(r["ts"])
                    r["open"] = float(r["open"])
                    r["close"] = float(r["close"])
            else:
                return jsonify({"error": "unsupported agg: " + agg}), 400
        conn.close()
        return jsonify({"symbol": norm, "from": from_ts, "to": to_ts, "agg": agg, "count": len(rows), "data": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/hist/stats', methods=['GET'])
def api_hist_stats():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, COUNT(*) AS cnt, MIN(tick_time) AS first_ts, MAX(tick_time) AS last_ts "
                "FROM tick_logs GROUP BY symbol ORDER BY cnt DESC"
            )
            rows = cur.fetchall()
            for r in rows:
                r["first_ts"] = int(r["first_ts"])
                r["last_ts"] = int(r["last_ts"])
        conn.close()
        return jsonify({"symbols": rows, "total_symbols": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== 兼容旧 /api/tick ====================
@app.route('/api/tick', methods=['POST'])
def receive_tick():
    try:
        ticks = request.json
        if not isinstance(ticks, list):
            ticks = [ticks]
        for t in ticks:
            _process_tick(t)
        return '', 204
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== 调试 ====================
@app.route("/api/debug/symbols", methods=["GET"])
def api_debug_symbols():
    raw_syms = {}
    with history_lock:
        for r in history_report:
            p = r.get("parsed")
            if isinstance(p, dict) and p.get("symbol"):
                raw_syms[p["symbol"]] = normalize_symbol(p["symbol"])
    with quotes_lock:
        cached = {k: {"bid": v["bid"], "ask": v["ask"], "ts": v["ts"]} for k, v in latest_quotes.items()}
    return jsonify({"raw_to_normalized": raw_syms, "known_count": len(KNOWN_SYMBOLS), "cached_quotes": cached})


@app.route("/api/debug/db", methods=["GET"])
def api_debug_db():
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM tick_logs")
            total = cur.fetchone()["total"]
            cur.execute("SELECT COUNT(DISTINCT symbol) AS syms FROM tick_logs")
            syms = cur.fetchone()["syms"]
        conn.close()
        with _buf_lock:
            buf_size = len(_write_buf)
        return jsonify({"db_ok": True, "total_rows": total, "distinct_symbols": syms, "buffer_pending": buf_size})
    except Exception as e:
        return jsonify({"db_ok": False, "error": str(e)})


# ==================== 最新状态 ====================
@app.route("/api/latest_status", methods=["GET"])
def api_latest_status():
    sf = request.args.get("symbol", "").upper()
    detail = None
    latest_quote = None

    if sf:
        with quotes_lock:
            q = latest_quotes.get(sf)
            if q:
                latest_quote = {
                    "bid": q["bid"],
                    "ask": q["ask"],
                    "symbol": sf,
                    "spread": q["spread"],
                    "ts": q["ts"],
                    "received_at": q["received_at"],
                }

    with history_lock:
        sr = history_status[0] if history_status else None
        pr = history_positions[0] if history_positions else None

        if not latest_quote:
            for record in history_report:
                p = record.get("parsed")
                if not isinstance(p, dict):
                    continue
                if p.get("desc") == "QUOTE_DATA":
                    rs = p.get("symbol", "")
                    if sf and not _symbol_matches(rs, sf):
                        continue
                    try:
                        qd = json.loads(p.get("message", "{}"))
                        if isinstance(qd, dict) and "bid" in qd:
                            latest_quote = {
                                "bid": qd["bid"],
                                "ask": qd["ask"],
                                "symbol": normalize_symbol(rs) or rs,
                                "spread": p.get("spread"),
                                "ts": p.get("ts"),
                                "received_at": record.get("received_at"),
                            }
                            break
                    except Exception:
                        pass

            if not latest_quote:
                for record in history_report:
                    p = record.get("parsed")
                    if not isinstance(p, dict):
                        continue
                    rs = p.get("symbol", "")
                    if sf and not _symbol_matches(rs, sf):
                        continue
                    if p.get("bid") is not None and p.get("ask") is not None:
                        latest_quote = {
                            "bid": p["bid"],
                            "ask": p["ask"],
                            "symbol": normalize_symbol(rs) or rs,
                            "spread": p.get("spread"),
                            "ts": p.get("ts"),
                            "received_at": record.get("received_at"),
                        }
                        break

        if sr:
            pd = sr.get("parsed", {})
            detail = {k: pd.get(k) for k in ["account", "server", "balance", "equity", "margin", "free_margin", "margin_level", "floating_pnl", "leverage_used"]}
            detail["received_at"] = sr.get("received_at")
            detail["positions"] = pd.get("positions") or (pr.get("parsed", {}).get("positions") if pr else [])
        elif pr:
            pd = pr.get("parsed", {})
            detail = {
                "received_at": pr.get("received_at"),
                "account": pd.get("account"),
                "server": pd.get("server"),
                "positions": pd.get("positions") or [],
            }

    return jsonify({"detail": detail, "latest_quote": latest_quote})


@app.route("/api/quotes", methods=["GET"])
def api_quotes():
    with quotes_lock:
        result = {}
        for sym, q in latest_quotes.items():
            result[sym] = {"bid": q["bid"], "ask": q["ask"], "spread": q["spread"], "ts": q["ts"], "received_at": q["received_at"]}
    return jsonify(result)


# ==================== MT4 原有接口 ====================
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


# ==================== K线 & 产品 ====================
@app.route("/api/kline", methods=["GET"])
def api_kline():
    symbol = request.args.get("symbol", "XAUUSD").upper()
    tf = request.args.get("tf", "5min")
    if tf not in KLINE_MAX:
        tf = "5min"
    limit = min(request.args.get("limit", KLINE_MAX[tf], type=int), KLINE_MAX[tf])
    with get_kline_lock(symbol):
        bars = list(kline_data.get(symbol, {}).get(tf, []))
    return jsonify({"symbol": symbol, "tf": tf, "bars": bars[-limit:]})

@app.route("/api/products", methods=["GET"])
def api_products():
    return jsonify(PRODUCT_SPECS)


# ==================== 前端页面 ====================
HTML_TEMPLATE = """<!doctype html>
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
--safe-b:env(safe-area-inset-bottom);--grid:rgba(255,255,255,0.04);--cross:rgba(255,255,255,0.25);
--label-bg:#2b3139;--label-text:#eaecef;--empty-text:#5e6673;--modal-bg:rgba(0,0,0,.65)}
:root.light{--bg:#f5f5f5;--card:#ffffff;--card2:#f0f0f0;--text:#1a1a2e;--muted:#6b7280;
--line:#e0e0e0;--green:#16a34a;--red:#dc2626;--yellow:#d97706;--accent:#2563eb;
--grid:rgba(0,0,0,0.06);--cross:rgba(0,0,0,0.2);--label-bg:#e5e7eb;--label-text:#1a1a2e;
--empty-text:#9ca3af;--modal-bg:rgba(0,0,0,.35)}
:root.light .dot.ok{background:var(--green);box-shadow:0 0 6px var(--green)}
:root.light .dot.err{background:var(--red);box-shadow:0 0 6px var(--red)}
:root.light .sg.g{background:var(--green);box-shadow:0 0 4px var(--green)}
:root.light .sg.y{background:var(--yellow);box-shadow:0 0 4px var(--yellow)}
:root.light .sg.r{background:var(--red);box-shadow:0 0 4px var(--red)}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;outline:none}
html{font-size:16px}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
color:var(--text);background:var(--bg);line-height:1.5;overflow-x:hidden;transition:background .4s,color .4s}
.app{width:100%;max-width:72rem;margin:0 auto;min-height:100vh;padding:1rem;padding-bottom:calc(1rem + var(--safe-b))}
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
background:var(--card);padding:.875rem 1.25rem;border-radius:.75rem;border:1px solid var(--line);transition:background .4s}
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
display:flex;flex-direction:column;gap:1rem;transition:background .4s}
.pm{text-align:center}
.pv{font-size:2.25rem;font-weight:800;letter-spacing:-.5px;font-family:"SF Mono",monospace;transition:color .2s}
.pv.up{color:var(--green)}.pv.down{color:var(--red)}
.pr{display:flex;justify-content:space-between;align-items:center;padding:.4rem 0;border-bottom:1px solid var(--line)}
.pr:last-child{border-bottom:0}
.pl{font-size:.8rem;font-weight:600;color:var(--muted)}
.pvv{font-size:1rem;font-weight:700;font-family:"SF Mono",monospace}
.pvv.bid{color:var(--green)}.pvv.ask{color:var(--red)}
.cp{background:var(--card);border:1px solid var(--line);border-radius:.75rem;display:flex;
flex-direction:column;overflow:hidden;min-height:380px;transition:background .4s}
.ch{display:flex;align-items:center;justify-content:space-between;padding:.625rem 1rem;
border-bottom:1px solid var(--line);flex-shrink:0}
.ct{font-size:.8rem;font-weight:700;color:var(--muted)}.ci{font-size:.75rem;color:var(--muted);font-family:monospace}
.tft{display:flex;gap:.25rem}
.tf{padding:.2rem .625rem;border-radius:.25rem;border:1px solid var(--line);background:0;
color:var(--muted);font-weight:600;font-size:.75rem;cursor:pointer;transition:.15s}
.tf:hover{color:var(--text)}.tf.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.cw{flex:1;position:relative;min-height:0;cursor:crosshair}
#kc,#cc{position:absolute;inset:0;width:100%;height:100%}#cc{pointer-events:none}
.stb{background:var(--card);border:1px solid var(--line);border-radius:.75rem;padding:.625rem 1rem;
display:flex;justify-content:space-between;align-items:center;transition:background .4s}
.si{display:flex;align-items:center;gap:.375rem;font-size:.75rem;font-weight:600;color:var(--muted)}
.sg{width:.5rem;height:.5rem;border-radius:50%;display:inline-block}
.sg.g{background:var(--green);box-shadow:0 0 4px var(--green)}
.sg.y{background:var(--yellow);box-shadow:0 0 4px var(--yellow)}
.sg.r{background:var(--red);box-shadow:0 0 4px var(--red)}
.db-tag{font-size:.65rem;padding:.1rem .4rem;border-radius:.2rem;margin-left:.25rem;
background:var(--card2);color:var(--muted);border:1px solid var(--line)}
.db-tag.ok{color:var(--green);border-color:var(--green)}.db-tag.err{color:var(--red);border-color:var(--red)}
.mask{position:fixed;inset:0;background:var(--modal-bg);display:none;align-items:center;
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
.theme-tag{font-size:.65rem;padding:.15rem .4rem;border-radius:.2rem;background:var(--card2);
color:var(--muted);border:1px solid var(--line);margin-left:.5rem;cursor:pointer}
@media(max-width:768px){
.main{grid-template-columns:1fr}.cp{min-height:320px}.sr{flex-wrap:wrap;gap:.5rem}
.mask{align-items:flex-end;padding:0}
.mdl{width:100%;border-radius:1rem 1rem 0 0;animation:sU .25s ease-out;padding-bottom:var(--safe-b)}
@keyframes sU{from{transform:translateY(100%)}to{transform:translateY(0)}}}
</style>
</head>
<body>
<div class="app">
<div class="topbar">
<div class="logo">MT4 量化终端<span class="theme-tag" id="thTag" onclick="toggleTheme()">--</span></div>
<div class="conn"><span id="cT">等待连接...</span><span class="dot" id="cD"></span><span class="db-tag" id="dbTag">DB --</span></div>
</div>
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
<div class="pr"><span class="pl">Bid</span><span class="pvv bid" id="bP">--</span></div>
<div class="pr"><span class="pl">Mid</span><span class="pvv" id="mP2">--</span></div>
<div class="pr"><span class="pl">Ask</span><span class="pvv ask" id="aP">--</span></div>
<div class="pr"><span class="pl">点差</span><span class="pvv" id="sV" style="color:var(--accent)">--</span></div>
</div></div>
<div class="cp">
<div class="ch">
<div style="display:flex;align-items:center;gap:.75rem"><div class="ct">K 线图</div><div class="ci" id="oI"></div></div>
<div class="tft">
<button class="tf on" data-tf="5min" onclick="stf(this)">5m</button>
<button class="tf" data-tf="10min" onclick="stf(this)">10m</button>
<button class="tf" data-tf="1hour" onclick="stf(this)">1H</button>
</div></div>
<div class="cw" id="cW"><canvas id="kc"></canvas><canvas id="cc"></canvas></div>
</div></div>
<div class="stb">
<div class="si"><span class="sg g" id="lS"></span><span id="lT">延迟: --</span></div>
<div class="si"><span id="sT">点差: --</span></div>
<div class="si"><span id="uT">数据: --</span></div>
</div></div>
<div class="mask" id="msk" onclick="if(event.target.id==='msk')msk.style.display='none'">
<div class="mdl"><div class="mh"><span id="mTi">切换品种</span><button class="xb" onclick="msk.style.display='none'">✕</button></div>
<div class="mb" id="mLi"></div></div></div>
<script>
const $=id=>document.getElementById(id);
const CP={forex:["EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURGBP","EURJPY","GBPJPY","AUDJPY","EURAUD","EURCHF","GBPCHF","CHFJPY","CADJPY","AUDNZD","AUDCAD","AUDCHF","EURNZD","EURCAD","CADCHF","NZDJPY","GBPAUD","GBPCAD","GBPNZD","NZDCAD","NZDCHF","USDSGD","USDHKD","USDCNH"],metal:["XAUUSD","XAGUSD"],commodity:["UKOUSD","USOUSD"],index:["U30USD","NASUSD","SPXUSD","100GBP","D30EUR","E50EUR","H33HKD"],crypto:["BTCUSD","ETHUSD","LTCUSD","BCHUSD","XMRUSD","BNBUSD","SOLUSD","ADAUSD","DOGUSD","XSIUSD","AVEUSD","DSHUSD","RPLUSD","LNKUSD"],stock:["AAPL","AMZN","BABA","GOOGL","META","MSFT","NFLX","NVDA","TSLA","ABBV","ABNB","ABT","ADBE","AMD","AVGO","C","CRM","DIS","GS","INTC","JNJ","MA","MCD","KO","MMM","NIO","PLTR","SHOP","TSM","V"]};
const CN={forex:"外汇",metal:"贵金属",commodity:"大宗商品",index:"指数",crypto:"虚拟货币",stock:"股票"};
let S="EURUSD",C="forex",TF="5min",lastM=null,stag=0,sigP=0,cBars=[],L=null,themeManual=null;
function getUTC8Hour(){return new Date(Date.now()+8*3600000).getUTCHours()}
function isDarkTime(){const h=getUTC8Hour();return h>=20||h<6}
function applyTheme(){let dark;if(themeManual!==null)dark=(themeManual==='dark');else dark=isDarkTime();
const root=document.documentElement;if(dark){root.classList.remove('light');$('thTag').innerText='夜间'}
else{root.classList.add('light');$('thTag').innerText='日间'}
const meta=document.querySelector('meta[name="theme-color"]');if(meta)meta.content=dark?'#0b0e11':'#f5f5f5'}
function toggleTheme(){if(themeManual===null)themeManual=isDarkTime()?'light':'dark';
else if(themeManual==='dark')themeManual='light';else themeManual=null;applyTheme()}
applyTheme();setInterval(applyTheme,60000);
function dg(s){if(!s)return 2;const u=s.toUpperCase();
if(["XAUUSD","XAGUSD","UKOUSD","USOUSD","BTCUSD","ETHUSD"].includes(u))return 2;
if(u.endsWith("JPY"))return 3;
if(["DOGUSD","ADAUSD","RPLUSD","XSIUSD","LNKUSD"].includes(u))return 5;
if(["LTCUSD","SOLUSD","BCHUSD","XMRUSD","BNBUSD","AVEUSD","DSHUSD"].includes(u))return 2;
if(["U30USD","NASUSD","SPXUSD","100GBP","D30EUR","E50EUR","H33HKD"].includes(u))return 1;
const fx=["USD","GBP","EUR","JPY","CHF","AUD","NZD","CAD","HKD","SGD","CNH"];
if(!fx.some(c=>u.includes(c)))return 2;return 5}
function fm(n,d){return n!=null?parseFloat(n).toFixed(d):'--'}
function nS(lo,hi,mt){if(lo===hi){const d=lo===0?1:Math.abs(lo)*.01;lo-=d;hi+=d}
const r=hi-lo,p=r*.08;let mn=lo-p,mx=hi+p;const rs=(mx-mn)/(mt-1),mg=Math.pow(10,Math.floor(Math.log10(rs))),res=rs/mg;
let ns;if(res<=1)ns=mg;else if(res<=2)ns=2*mg;else if(res<=2.5)ns=2.5*mg;else if(res<=5)ns=5*mg;else ns=10*mg;
const tI=Math.floor(mn/ns)*ns,tA=Math.ceil(mx/ns)*ns,tk=[];for(let v=tI;v<=tA+ns*.5;v+=ns)tk.push(v);return{tI,tA,ns,tk}}
function tick(){const n=new Date();$('clk').innerText=[n.getHours(),n.getMinutes(),n.getSeconds()].map(v=>String(v).padStart(2,'0')).join(':')}
function stab(b,c){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));b.classList.add('on');C=c}
function openM(){$('mTi').innerText='切换'+(CN[C]||'品种');$('mLi').innerHTML=(CP[C]||[]).map(s=>'<div class="it '+(s===S?'on':'')+'" onclick="pick(\''+s+'\')"><span>'+s+'</span></div>').join('');$('msk').style.display='flex'}
function pick(s){S=s;$('sN').innerText=s;$('msk').style.display='none';['bP','aP','mP','mP2','sV','bB','bA'].forEach(function(id){if($(id))$(id).innerText='--'});$('sT').innerText='点差: --';lastM=null;stag=0;drawE();rf();fK()}
async function rf(){try{const r=await fetch('/api/latest_status?symbol='+S),d=await r.json();cOk(true);
if(d.latest_quote){const q=d.latest_quote,D=dg(S);
if(q.bid!=null&&q.ask!=null){const mid=(q.bid+q.ask)/2,prev=lastM;lastM=mid;
$('bP').innerText=fm(q.bid,D);$('aP').innerText=fm(q.ask,D);$('mP').innerText=fm(mid,D);$('mP2').innerText=fm(mid,D);
$('bB').innerText=fm(q.bid,D);$('bA').innerText=fm(q.ask,D);
const pe=$('mP');pe.classList.remove('up','down');
if(prev!=null){if(mid>prev)pe.classList.add('up');else if(mid<prev)pe.classList.add('down')}
const sp=q.spread!=null?q.spread:((q.ask-q.bid)*Math.pow(10,D>2?0:(5-D))).toFixed(1);
$('sV').innerText=sp;$('sT').innerText='点差: '+sp}
const now=Date.now();
if(q.ts!=null){const sMs=q.ts<1e12?q.ts*1000:q.ts,lat=Math.round(now-sMs-8*3600000);
$('lT').innerText='延迟: '+lat+'ms';lSig(lat);
const dt=new Date(sMs+8*3600000);
$('uT').innerText='数据: '+[dt.getUTCMonth()+1,dt.getUTCDate()].map(v=>String(v).padStart(2,'0')).join('-')+' '+[dt.getUTCHours(),dt.getUTCMinutes(),dt.getUTCSeconds()].map(v=>String(v).padStart(2,'0')).join(':')}
else if(q.received_at)$('uT').innerText='更新: '+(q.received_at.split(' ')[1]||'--')
}}catch(e){console.error(e);cOk(false)}}
async function fK(){try{const lm={_5min:300,_10min:250,_1hour:200};
const r=await fetch('/api/kline?symbol='+S+'&tf='+TF+'&limit='+(lm['_'+TF]||300)),d=await r.json();
if(d.bars&&d.bars.length>0){cBars=d.bars;drawK(d.bars)}else{cBars=[];drawE()}}catch(e){drawE()}}
function stf(b){document.querySelectorAll('.tf').forEach(t=>t.classList.remove('on'));b.classList.add('on');TF=b.dataset.tf;fK()}
function cv(name){return getComputedStyle(document.documentElement).getPropertyValue(name).trim()}
function drawK(bars){const cvs=$('kc');if(!cvs)return;const ctx=cvs.getContext('2d');
const dpr=devicePixelRatio||1,w=cvs.offsetWidth,h=cvs.offsetHeight;
if(w<=0||h<=0)return;cvs.width=w*dpr;cvs.height=h*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
if(!bars||!bars.length){drawE();return}
const G=cv('--green')||'#0ecb81',R=cv('--red')||'#f6465d',gridC=cv('--grid'),axC=cv('--muted');
const PL=8,PR=72,PT=16,PB=24,cW=w-PL-PR,cH=h-PT-PB;if(cW<=0||cH<=0)return;
const df={_5min:80,_10min:60,_1hour:48};let vn=Math.min(df['_'+TF]||80,bars.length,Math.floor(cW/6));
vn=Math.max(vn,Math.min(20,bars.length));const vb=bars.slice(-vn),n=vb.length;if(!n)return;
let dMin=Infinity,dMax=-Infinity;for(const b of vb){if(b[2]>dMax)dMax=b[2];if(b[3]<dMin)dMin=b[3]}
const D=dg(S),sc=nS(dMin,dMax,6),yI=sc.tI,yA=sc.tA,yR=yA-yI||1;
const p2y=p=>PT+cH*(1-(p-yI)/yR),y2p=y=>yI+(1-(y-PT)/cH)*yR;
const bT=cW/n,gap=Math.max(1,Math.round(bT*.2)),bW=Math.max(1,Math.floor(bT-gap)),hB=bW/2;
const bx=i=>PL+(i+.5)*bT;L={PL,PR,PT,PB,cW,cH,n,bT,yI,yA,yR,p2y,y2p,bx,D,vb};
ctx.font='11px "SF Mono",Menlo,monospace';ctx.textAlign='left';ctx.textBaseline='middle';
for(const t of sc.tk){const y=p2y(t);if(y<PT-2||y>PT+cH+2)continue;
ctx.strokeStyle=gridC;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(PL,Math.round(y)+.5);ctx.lineTo(PL+cW,Math.round(y)+.5);ctx.stroke();
ctx.fillStyle=axC;ctx.fillText(t.toFixed(D),PL+cW+8,y)}
for(let i=0;i<n;i++){const b=vb[i],o=b[1],hi=b[2],lo=b[3],cl=b[4];
const up=cl>=o,col=up?G:R,x=bx(i);
ctx.strokeStyle=col;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(Math.round(x)+.5,Math.round(p2y(hi)));ctx.lineTo(Math.round(x)+.5,Math.round(p2y(lo)));ctx.stroke();
const yO=p2y(o),yC=p2y(cl),bt=Math.min(yO,yC),bb=Math.max(yO,yC);
ctx.fillStyle=col;ctx.fillRect(Math.round(x-hB),Math.round(bt),bW,Math.max(1,bb-bt))}
if(n>0){const lc=vb[n-1][4],up=lc>=vb[n-1][1],col=up?G:R,yL=p2y(lc);
ctx.setLineDash([4,3]);ctx.strokeStyle=col;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(PL,Math.round(yL)+.5);ctx.lineTo(PL+cW,Math.round(yL)+.5);ctx.stroke();ctx.setLineDash([]);
const tW=PR-6,tH=18,tX=PL+cW+2,tY=Math.round(yL)-tH/2;
ctx.fillStyle=col;ctx.beginPath();ctx.roundRect(tX,tY,tW,tH,3);ctx.fill();
ctx.fillStyle='#fff';ctx.font='bold 11px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText(lc.toFixed(D),tX+tW/2,Math.round(yL))}
ctx.fillStyle=axC;ctx.font='10px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='top';
const mG=60,iMs=TF==='1hour'?3600000*4:TF==='10min'?3600000:1800000;let lX=-Infinity;
for(let i=0;i<n;i++){const ts=vb[i][0],x=bx(i);
if(ts%iMs!==0&&i!==0&&i!==n-1)continue;if(x-lX<mG)continue;
const d=new Date(ts+8*3600000);let lb;
if(TF==='1hour')lb=(d.getUTCMonth()+1)+'/'+d.getUTCDate()+' '+String(d.getUTCHours()).padStart(2,'0')+':00';
else lb=String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0');
ctx.strokeStyle=gridC;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(Math.round(x)+.5,PT+cH);ctx.lineTo(Math.round(x)+.5,PT+cH+4);ctx.stroke();
ctx.fillStyle=axC;ctx.fillText(lb,x,PT+cH+6);lX=x}clrCH()}
function drawE(){const cvs=$('kc');if(!cvs)return;const ctx=cvs.getContext('2d'),dpr=devicePixelRatio||1,w=cvs.offsetWidth,h=cvs.offsetHeight;
cvs.width=w*dpr;cvs.height=h*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
ctx.fillStyle=cv('--empty-text')||'#5e6673';ctx.font='13px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText('暂无 K 线数据',w/2,h/2);clrCH();L=null}
function clrCH(){const cvs=$('cc');if(!cvs)return;const dpr=devicePixelRatio||1,w=cvs.offsetWidth,h=cvs.offsetHeight;
cvs.width=w*dpr;cvs.height=h*dpr;cvs.getContext('2d').scale(dpr,dpr);$('oI').innerText=''}
function dCH(mx,my){const cvs=$('cc');if(!cvs||!L)return;
const dpr=devicePixelRatio||1,w=cvs.offsetWidth,h=cvs.offsetHeight;
cvs.width=w*dpr;cvs.height=h*dpr;const ctx=cvs.getContext('2d');ctx.scale(dpr,dpr);
const{PL,PT,cW,cH,n,bT,D,vb,p2y,y2p,bx}=L;
const crossC=cv('--cross'),lblBg=cv('--label-bg'),lblTx=cv('--label-text');
const cx=Math.max(PL,Math.min(mx,PL+cW)),cy=Math.max(PT,Math.min(my,PT+cH));
const idx=Math.max(0,Math.min(Math.round((cx-PL)/bT-.5),n-1)),sx=bx(idx);
ctx.setLineDash([3,3]);ctx.strokeStyle=crossC;ctx.lineWidth=1;
ctx.beginPath();ctx.moveTo(Math.round(sx)+.5,PT);ctx.lineTo(Math.round(sx)+.5,PT+cH);ctx.stroke();
ctx.beginPath();ctx.moveTo(PL,Math.round(cy)+.5);ctx.lineTo(PL+cW,Math.round(cy)+.5);ctx.stroke();ctx.setLineDash([]);
const pr=y2p(cy);ctx.fillStyle=lblBg;
const tW2=64,tH2=18,tX2=PL+cW+2,tY2=Math.round(cy)-tH2/2;
ctx.beginPath();ctx.roundRect(tX2,tY2,tW2,tH2,3);ctx.fill();
ctx.fillStyle=lblTx;ctx.font='11px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText(pr.toFixed(D),tX2+tW2/2,Math.round(cy));
if(idx>=0&&idx<n){const b=vb[idx],ts=b[0],d=new Date(ts+8*3600000);
let lb;if(TF==='1hour')lb=(d.getUTCMonth()+1)+'/'+d.getUTCDate()+' '+String(d.getUTCHours()).padStart(2,'0')+':00';
else lb=String(d.getUTCHours()).padStart(2,'0')+':'+String(d.getUTCMinutes()).padStart(2,'0');
const tw=ctx.measureText(lb).width+12,tx=Math.round(sx)-tw/2,ty=PT+cH+2;
ctx.fillStyle=lblBg;ctx.beginPath();ctx.roundRect(tx,ty,tw,16,3);ctx.fill();
ctx.fillStyle=lblTx;ctx.font='10px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='top';
ctx.fillText(lb,Math.round(sx),ty+2);
const o=b[1],hi=b[2],lo=b[3],cl=b[4],up=cl>=o;
const c2=up?(cv('--green')||'#0ecb81'):(cv('--red')||'#f6465d');
$('oI').innerHTML='<span style="color:'+c2+'">O:'+o.toFixed(D)+' H:'+hi.toFixed(D)+' L:'+lo.toFixed(D)+' C:'+cl.toFixed(D)+'</span>'}}
(function(){const wr=$('cW');if(!wr)return;
function pos(e){const r=wr.getBoundingClientRect();return e.touches?{x:e.touches[0].clientX-r.left,y:e.touches[0].clientY-r.top}:{x:e.clientX-r.left,y:e.clientY-r.top}}
wr.addEventListener('mousemove',function(e){var p=pos(e);dCH(p.x,p.y)});
wr.addEventListener('mouseleave',function(){clrCH()});
wr.addEventListener('touchmove',function(e){e.preventDefault();var p=pos(e);dCH(p.x,p.y)},{passive:false});
wr.addEventListener('touchend',function(){clrCH()})})();
function lSig(v){const s=$('lS');if(!s)return;s.className='sg';if(v<500)s.classList.add('g');else if(v<2000)s.classList.add('y');else s.classList.add('r')}
function cOk(ok){const d=$('cD'),t=$('cT');if(!d||!t)return;d.className='dot';if(ok){d.classList.add('ok');t.innerText='已连接'}else{d.classList.add('err');t.innerText='连接断开'}}
function chkS(){const p=parseFloat(($('mP')||{}).innerText)||0;if(sigP===p&&p>0){stag++;if(stag>=5){const s=$('lS');if(s){s.className='sg';s.classList.add('r')}$('lT').innerText='数据停滞!'}}else stag=0;sigP=p}
async function chkDB(){try{const r=await fetch('/api/debug/db'),d=await r.json();const tag=$('dbTag');if(!tag)return;if(d.db_ok){tag.className='db-tag ok';tag.innerText='DB '+d.total_rows}else{tag.className='db-tag err';tag.innerText='DB x'}}catch(e){const tag=$('dbTag');if(tag){tag.className='db-tag err';tag.innerText='DB x'}}}
tick();setInterval(tick,1000);rf();setInterval(rf,2000);setInterval(chkS,2000);fK();setInterval(fK,2000);chkDB();setInterval(chkDB,15000);
addEventListener('resize',function(){clearTimeout(window._rt);window._rt=setTimeout(function(){if(cBars.length)drawK(cBars);else drawE()},100)});
</script>
</body></html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


# ==================== 启动 ====================
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
