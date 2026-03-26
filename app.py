import os
import json
import threading
import traceback
import time
import re
from datetime import datetime
from zoneinfo import ZoneInfo

# ==================== 东八区时间工具 ====================
SH_TZ = ZoneInfo("Asia/Shanghai")

def now_shanghai_dt():
    """返回东八区当前 datetime 对象"""
    return datetime.now(SH_TZ)

def now_shanghai_str():
    """返回东八区时间字符串，精确到毫秒"""
    return now_shanghai_dt().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
from collections import deque
from flask import Flask, request, render_template_string, jsonify

# ==================== MySQL (optional) ====================
try:
    import pymysql
    from pymysql.cursors import DictCursor
    from dbutils.pooled_db import PooledDB
    HAS_MYSQL = True
except ImportError:
    HAS_MYSQL = False
    print("[DB] pymysql/dbutils not installed, MySQL features disabled")

app = Flask(__name__)

# ==================== MySQL 连接池 (可选) ====================
import os as _os
MYSQL_HOST = _os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(_os.environ.get("MYSQL_PORT", 3306))
MYSQL_USER = _os.environ.get("MYSQL_USER", "root")
MYSQL_PASS = _os.environ.get("MYSQL_PASSWORD", "")
MYSQL_DB   = _os.environ.get("MYSQL_DATABASE", "mt4data")

_pool = None
_db_ok = False

def get_pool():
    global _pool
    if not HAS_MYSQL:
        return None
    if _pool is None:
        _pool = PooledDB(
            creator=pymysql, maxconnections=10, mincached=0, maxcached=5,
            blocking=False, host=MYSQL_HOST, port=MYSQL_PORT,
            user=MYSQL_USER, passwd=MYSQL_PASS, db=MYSQL_DB,
            charset="utf8mb4", cursorclass=DictCursor, autocommit=True,
            connect_timeout=3,
        )
    return _pool

def get_conn():
    p = get_pool()
    if p is None:
        raise RuntimeError("MySQL not available")
    return p.connection()

def init_db():
    global _db_ok
    if not HAS_MYSQL:
        return
    try:
        conn = get_conn()
        print(f"[DB] connecting to {MYSQL_HOST}:{MYSQL_PORT} as {MYSQL_USER} / db={MYSQL_DB}")
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tick_logs (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    bid DOUBLE NOT NULL, ask DOUBLE NOT NULL,
                    spread DOUBLE DEFAULT 0, mid DOUBLE DEFAULT 0,
                    tick_time BIGINT NOT NULL,
                    received_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                    INDEX idx_sym_time (symbol, tick_time),
                    INDEX idx_time (tick_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quote_ticks (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    bid DOUBLE NOT NULL,
                    ask DOUBLE NOT NULL,
                    spread DOUBLE DEFAULT 0,
                    received_at DATETIME(3) NOT NULL,
                    INDEX idx_symbol_time (symbol, received_at),
                    INDEX idx_received_at (received_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS latest_quotes (
                    symbol VARCHAR(20) PRIMARY KEY,
                    bid DOUBLE NOT NULL,
                    ask DOUBLE NOT NULL,
                    spread DOUBLE DEFAULT 0,
                    received_at DATETIME(3) NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.close()
        _db_ok = True
        print("[DB] tick_logs ready")
        print("[DB] quote_ticks ready (5-field template)")
        print("[DB] latest_quotes ready (snapshot)")
    except Exception as e:
        _db_ok = False
        print(f"[DB] init failed: {e}")

def rebuild_kline_from_db():
    """服务启动时从 MySQL quote_ticks 表重建内存 K 线缓存"""
    global _db_ok
    if not HAS_MYSQL:
        return

    # ★ 修复：等待 init_db 完成，最多等 10 秒
    for _ in range(20):
        if _db_ok:
            break
        time.sleep(0.5)

    if not _db_ok:
        print("[Kline] skipped rebuild: DB not ready after waiting")
        return

    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, bid, ask,
                       UNIX_TIMESTAMP(received_at)*1000 AS ts_ms
                FROM quote_ticks
                WHERE received_at >= NOW() - INTERVAL 24 HOUR
                ORDER BY received_at ASC
            """)
            rows = cur.fetchall()
        conn.close()

        for r in rows:
            mid = (r['bid'] + r['ask']) / 2.0
            update_kline(r['symbol'], r['bid'], r['ask'], int(r['ts_ms']))
        print(f"[Kline] rebuilt from DB: {len(rows)} ticks")
    except Exception as e:
        print(f"[Kline] rebuild failed: {e}")

threading.Thread(target=init_db, daemon=True).start()
threading.Thread(target=rebuild_kline_from_db, daemon=True).start()

# ---- 写入缓冲 ----
_write_buf = []
_buf_lock = threading.Lock()
_last_flush = time.time()

def _buf_append(row):
    global _last_flush
    if not HAS_MYSQL:
        return
    with _buf_lock:
        _write_buf.append(row)
        if len(_write_buf) >= 50:
            _flush_buf()

def _flush_buf():
    global _write_buf, _last_flush
    if not _write_buf:
        return
    batch = _write_buf[:]
    _write_buf = []
    _last_flush = time.time()
    threading.Thread(target=_do_insert, args=(batch,), daemon=True).start()

def _do_insert(batch, retry_count=0):
    global _db_ok
    if not batch:
        return
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            rows = [(d["symbol"], d["bid"], d["ask"], d.get("spread") or 0.0, d["received_at"]) for d in batch]
            cur.executemany(
                "INSERT INTO quote_ticks (symbol,bid,ask,spread,received_at) "
                "VALUES (%s,%s,%s,%s,%s)", rows)
            upsert = (
                "INSERT INTO latest_quotes (symbol,bid,ask,spread,received_at) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE "
                "bid=VALUES(bid), ask=VALUES(ask), spread=VALUES(spread), received_at=VALUES(received_at)"
            )
            cur.executemany(upsert, rows)
        conn.close()
        _db_ok = True
    except Exception as e:
        _db_ok = False
        print(f"[DB] _do_insert failed ({len(batch)}): {repr(e)}")
        if retry_count < 1:
            print(f"[DB] batch requeued (retry {retry_count + 1})")
            def requeue():
                global _write_buf
                with _buf_lock:
                    _write_buf = batch + _write_buf
            requeue()
        else:
            print(f"[DB] batch dropped after max retries ({len(batch)} rows)")

def _flush_timer():
    while True:
        time.sleep(2)
        with _buf_lock:
            if _write_buf and (time.time() - _last_flush) >= 2:
                _flush_buf()

threading.Thread(target=_flush_timer, daemon=True).start()

# ---- Diagnostics ----
_tick_count = 0
_tick_count_lock = threading.Lock()
_last_tick_at = None

# ==================== 全局数据结构 ====================
MAX_HISTORY = 50
KLINE_MAX = {"5min": 300, "10min": 250, "1hour": 200}
kline_data = {}
kline_locks = {}
kline_locks_guard = threading.Lock()

KNOWN_SYMBOLS = set()

def get_kline_lock(symbol):
    if symbol not in kline_locks:
        with kline_locks_guard:
            if symbol not in kline_locks:
                kline_locks[symbol] = threading.Lock()
    return kline_locks[symbol]

def normalize_symbol(raw_symbol: str) -> str:
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

def _floor_ts(ts_ms, tf):
    if tf == "5min":     step = 5 * 60 * 1000
    elif tf == "10min":  step = 10 * 60 * 1000
    elif tf == "1hour":  step = 60 * 60 * 1000
    else:                step = 60 * 1000
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

latest_quote_cache = {}
quote_cache_lock = threading.Lock()

def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

# ==================== 5字段报价模板 ====================
def make_quote_row(raw_symbol, bid, ask, spread=None):
    norm = normalize_symbol(raw_symbol or "")
    if not norm:
        return None
    bf = _to_float(bid)
    af = _to_float(ask)
    if bf is None or af is None:
        return None
    sp = _to_float(spread)
    if sp is None:
        sp = 0.0
    return {
        "symbol": norm,
        "bid": bf,
        "ask": af,
        "spread": sp,
        "received_at": now_shanghai_str(),
    }

# ==================== 时间戳工具 ====================
SOURCE_TS_OFFSET_HOURS = int(os.environ.get("SOURCE_TS_OFFSET_HOURS", "0"))
SOURCE_TS_OFFSET_MS = SOURCE_TS_OFFSET_HOURS * 3600 * 1000

def to_tick_ts_ms(ts_value, apply_source_offset=True):
    if ts_value is None:
        return None
    try:
        v = float(ts_value)
    except (TypeError, ValueError):
        return None
    result = int(v) if v > 1e12 else int(v * 1000)
    if apply_source_offset and SOURCE_TS_OFFSET_MS:
        result -= SOURCE_TS_OFFSET_MS
    return result

def ts_ms_to_shanghai_str(ts_ms):
    if ts_ms is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=SH_TZ)
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except (ValueError, OSError, OverflowError):
        return ""

def ts_ms_to_db_str(ts_ms):
    return ts_ms_to_shanghai_str(ts_ms)

def build_latency_fields(ts_value):
    quote_ts_ms = to_tick_ts_ms(ts_value)
    now_dt = now_shanghai_dt()
    server_ts_ms = int(now_dt.timestamp() * 1000)
    server_time_shanghai = now_shanghai_str()
    if quote_ts_ms is not None:
        latency_ms = server_ts_ms - quote_ts_ms
        is_stale = latency_ms > 3000
    else:
        latency_ms = None
        is_stale = True
    return {
        "quote_ts_ms": quote_ts_ms,
        "quote_time_shanghai": ts_ms_to_shanghai_str(quote_ts_ms) if quote_ts_ms else "",
        "server_ts_ms": server_ts_ms,
        "server_time_shanghai": server_time_shanghai,
        "latency_ms": latency_ms,
        "is_stale": is_stale,
    }

def build_live_quote_payload(q):
    if not isinstance(q, dict):
        return None
    sym = q.get("symbol")
    bid = _to_float(q.get("bid"))
    ask = _to_float(q.get("ask"))
    if not sym or bid is None or ask is None:
        return None
    ts_val = q.get("ts")
    lat = build_latency_fields(ts_val)
    return {
        "symbol": sym,
        "bid": bid,
        "ask": ask,
        "spread": _to_float(q.get("spread")),
        "received_at": q.get("received_at") or "",
        **lat,
    }

def _bid_ask_from_dict(d):
    if not isinstance(d, dict):
        return None, None
    for bk, ak in (("bid", "ask"), ("Bid", "Ask"), ("BID", "ASK")):
        b, a = _to_float(d.get(bk)), _to_float(d.get(ak))
        if b is not None and a is not None:
            return b, a
    return None, None

def _message_to_quote_dict(p):
    m = p.get("message")
    if m is None:
        return None
    if isinstance(m, dict):
        return m
    if isinstance(m, str) and m.strip():
        try:
            o = json.loads(m)
            return o if isinstance(o, dict) else None
        except Exception:
            return None
    return None

def cache_tick_quote(raw_symbol, bid, ask, spread=None, ts=None):
    norm = normalize_symbol(raw_symbol or "")
    if not norm:
        return
    b, a = _to_float(bid), _to_float(ask)
    if b is None or a is None:
        return
    rec = {
        "bid": b,
        "ask": a,
        "symbol": norm,
        "spread": spread,
        "ts": ts,
        "received_at": now_shanghai_str(),
    }
    with quote_cache_lock:
        latest_quote_cache[norm] = rec

def ingest_quote_from_parsed(p):
    if not isinstance(p, dict):
        return
    rs = p.get("symbol") or p.get("Symbol") or ""
    b, a = _bid_ask_from_dict(p)
    qd = None
    if b is None or a is None:
        qd = _message_to_quote_dict(p)
        if qd:
            b, a = _bid_ask_from_dict(qd)
    if b is None or a is None:
        return
    sp = p.get("spread")
    if sp is None and qd is not None:
        sp = qd.get("spread")
    ts = p.get("ts") or p.get("time") or p.get("Time")
    cache_tick_quote(rs, b, a, spread=sp, ts=ts)

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
    if not filter_symbol: return True
    if not record_symbol_raw: return False
    return normalize_symbol(record_symbol_raw) == filter_symbol

def store_mt4_data(raw_body, client_ip, headers_dict):
    parsed_json, parse_error, parse_error_detail, remaining_data = try_parse_json(raw_body)
    category = detect_category(request.path, parsed_json if isinstance(parsed_json, dict) else None)
    record = {
        "received_at": now_shanghai_str(),
        "ip": client_ip, "method": request.method, "path": request.path,
        "category": category, "headers": headers_dict, "body_raw": raw_body,
        "parsed": parsed_json, "parse_error": parse_error,
        "parse_error_detail": parse_error_detail, "remaining_data": remaining_data,
    }
    if isinstance(parsed_json, dict):
        for k in ["account","server","balance","equity","floating_pnl","leverage_used","risk_flags","exposure_notional","positions"]:
            record[k] = parsed_json.get(k)
        if category == "report" or request.path.rstrip("/").endswith("/mt4/quote"):
            ingest_quote_from_parsed(parsed_json)
    with history_lock:
        {"status":history_status,"positions":history_positions,"report":history_report,
         "poll":history_poll,"echo":history_echo}.get(category, history_echo).appendleft(record)
    return parsed_json, record

# ==================== 调试接口 ====================
@app.route("/api/debug/symbols", methods=["GET"])
def api_debug_symbols():
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
    sf = request.args.get("symbol", "").strip().upper()
    detail = latest_quote = None
    if sf:
        nk = normalize_symbol(sf)
        with quote_cache_lock:
            cq = latest_quote_cache.get(nk)
        if cq:
            latest_quote = dict(cq)
    with history_lock:
        sr = history_status[0] if history_status else None
        pr = history_positions[0] if history_positions else None

        if not latest_quote:
            for record in history_report:
                p = record.get("parsed")
                if not isinstance(p, dict): continue
                if p.get("desc") == "QUOTE_DATA":
                    rs = p.get("symbol", "")
                    if sf and not _symbol_matches(rs, sf): continue
                    qd = _message_to_quote_dict(p)
                    if not qd:
                        try:
                            qd = json.loads(p.get("message") or "{}")
                        except Exception:
                            qd = None
                    if isinstance(qd, dict):
                        b, a = _bid_ask_from_dict(qd)
                        if b is not None and a is not None:
                            latest_quote = {"bid": b, "ask": a,
                                "symbol": normalize_symbol(rs) or rs,
                                "spread": p.get("spread"), "ts": p.get("ts"),
                                "received_at": record.get("received_at")}
                            break

        if not latest_quote:
            for record in history_report:
                p = record.get("parsed")
                if not isinstance(p, dict): continue
                rs = p.get("symbol", "")
                if sf and not _symbol_matches(rs, sf): continue
                b, a = _bid_ask_from_dict(p)
                if b is not None and a is not None:
                    latest_quote = {"bid": b, "ask": a,
                        "symbol": normalize_symbol(rs) or rs,
                        "spread": p.get("spread"), "ts": p.get("ts"),
                        "received_at": record.get("received_at")}
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

    if latest_quote and isinstance(latest_quote, dict):
        latest_quote.update(build_latency_fields(latest_quote.get("ts")))

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
def _handle_tick_list(ticks):
    """Shared logic: memory + MySQL for each tick"""
    count = 0
    for tick in ticks:
        sym_raw = tick.get('symbol')
        bid, ask = tick.get('bid'), tick.get('ask')
        tt = tick.get('tick_time')
        if not sym_raw or bid is None or ask is None:
            continue
        bf, af = _to_float(bid), _to_float(ask)
        if bf is None or af is None:
            continue

        ts_ms = to_tick_ts_ms(tt)
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)

        spread = _to_float(tick.get('spread')) or 0

        # 1) memory: kline + quote cache
        update_kline(sym_raw, bf, af, ts_ms)
        cache_tick_quote(sym_raw, bf, af, spread=tick.get('spread'), ts=tt)

        # 2) history_report (frontend compat)
        record = {
            "received_at": now_shanghai_str(),
            "ip": get_client_ip(), "method": "POST", "path": request.path,
            "category": "report", "headers": {}, "body_raw": json.dumps(tick),
            "parsed": {"desc":"QUOTE_DATA","spread":tick.get('spread',0),"ts":tt,
                "message":json.dumps({"bid":bf,"ask":af}),
                "symbol":sym_raw,"account":"tick_stream"}
        }
        with history_lock:
            history_report.appendleft(record)

        # 3) MySQL buffer
        quote_row = make_quote_row(sym_raw, bid, ask, spread=spread)
        if quote_row:
            _buf_append(quote_row)

        # 4) counter
        global _tick_count, _last_tick_at
        with _tick_count_lock:
            _tick_count += 1
            _last_tick_at = now_shanghai_str()

        count += 1
    return count

@app.route('/api/tick', methods=['POST'])
def receive_tick():
    try:
        ticks = request.json
        if not isinstance(ticks, list): ticks = [ticks]
        _handle_tick_list(ticks)
        return '', 204
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tick-ren', methods=['POST'])
def tick_ren():
    try:
        ticks = request.json
        if not isinstance(ticks, list): ticks = [ticks]
        count = _handle_tick_list(ticks)
        return jsonify({"ok": True, "processed": count}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/api/tick-hist', methods=['POST'])
def tick_hist():
    """Historical batch import"""
    if not HAS_MYSQL:
        return jsonify({"ok": False, "error": "MySQL not available"}), 503
    try:
        ticks = request.json
        if not isinstance(ticks, list): ticks = [ticks]
        old_rows = []
        new_rows = []
        for tick in ticks:
            sym_raw = tick.get('symbol')
            bid, ask = tick.get('bid'), tick.get('ask')
            tt = tick.get('tick_time')
            if not sym_raw or bid is None or ask is None: continue
            bf, af = _to_float(bid), _to_float(ask)
            if bf is None or af is None: continue
            spread = _to_float(tick.get('spread')) or 0

            ts_ms = to_tick_ts_ms(tt)
            if ts_ms is None:
                ts_ms = int(time.time() * 1000)

            mid = (bf + af) / 2.0
            norm = normalize_symbol(sym_raw)
            now_str = now_shanghai_str()
            old_rows.append((norm, bf, af, spread, mid, ts_ms, now_str))
            qr = make_quote_row(sym_raw, bid, ask, spread=spread)
            if qr:
                new_rows.append((qr["symbol"], qr["bid"], qr["ask"], qr["spread"], qr["received_at"]))
        conn = get_conn()
        with conn.cursor() as cur:
            if old_rows:
                cur.executemany(
                    "INSERT INTO tick_logs (symbol,bid,ask,spread,mid,tick_time,received_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)", old_rows)
            if new_rows:
                cur.executemany(
                    "INSERT INTO quote_ticks (symbol,bid,ask,spread,received_at) "
                    "VALUES (%s,%s,%s,%s,%s)", new_rows)
                upsert = (
                    "INSERT INTO latest_quotes (symbol,bid,ask,spread,received_at) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE "
                    "bid=VALUES(bid), ask=VALUES(ask), spread=VALUES(spread), received_at=VALUES(received_at)"
                )
                cur.executemany(upsert, new_rows)
        conn.close()
        return jsonify({"ok": True, "inserted": len(old_rows)}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ==================== 历史查询 ====================
def _parse_ts(val):
    if not val: return None
    val = val.strip()
    if val.isdigit():
        v = int(val)
        return v if v > 1e12 else v * 1000
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(val, fmt).replace(tzinfo=SH_TZ)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None

@app.route('/api/hist', methods=['GET'])
def api_hist():
    if not HAS_MYSQL or not _db_ok:
        return jsonify({"error": "MySQL not available"}), 503
    symbol = request.args.get("symbol", "").upper()
    if not symbol: return jsonify({"error": "symbol required"}), 400
    norm = normalize_symbol(symbol)
    limit = min(int(request.args.get("limit", 1000)), 10000)
    agg = request.args.get("agg", "raw")
    now_sh = now_shanghai_dt()
    default_from = int(now_sh.timestamp() * 1000) - 3600000
    default_to = int(now_sh.timestamp() * 1000) + 60000
    from_ts = _parse_ts(request.args.get("from")) or default_from
    to_ts = _parse_ts(request.args.get("to")) or default_to
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            if agg == "raw":
                cur.execute(
                    "SELECT symbol,bid,ask,spread,mid,tick_time,received_at FROM tick_logs "
                    "WHERE symbol=%s AND tick_time>=%s AND tick_time<=%s ORDER BY tick_time ASC LIMIT %s",
                    (norm, from_ts, to_ts, limit))
                rows = cur.fetchall()
                for r in rows:
                    if isinstance(r.get("received_at"), datetime):
                        r["received_at"] = r["received_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            elif agg in ("1s","5s","1min"):
                step = {"1s":1000,"5s":5000,"1min":60000}[agg]
                cur.execute(
                    "SELECT FLOOR(tick_time/%s)*%s AS ts,"
                    "SUBSTRING_INDEX(GROUP_CONCAT(mid ORDER BY tick_time ASC),',',1)+0 AS open,"
                    "MAX(mid) AS high,MIN(mid) AS low,"
                    "SUBSTRING_INDEX(GROUP_CONCAT(mid ORDER BY tick_time DESC),',',1)+0 AS close,"
                    "COUNT(*) AS ticks,AVG(spread) AS avg_spread "
                    "FROM tick_logs WHERE symbol=%s AND tick_time>=%s AND tick_time<=%s "
                    "GROUP BY FLOOR(tick_time/%s) ORDER BY ts ASC LIMIT %s",
                    (step,step,norm,from_ts,to_ts,step,limit))
                rows = cur.fetchall()
                for r in rows:
                    r["ts"]=int(r["ts"]); r["open"]=float(r["open"]); r["close"]=float(r["close"])
            else:
                return jsonify({"error":"unsupported agg"}),400
        conn.close()
        return jsonify({"symbol":norm,"from":from_ts,"to":to_ts,"agg":agg,"count":len(rows),"data":rows})
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route('/api/hist/stats', methods=['GET'])
def api_hist_stats():
    if not HAS_MYSQL or not _db_ok:
        return jsonify({"error": "MySQL not available"}), 503
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT symbol,COUNT(*) AS cnt,MIN(tick_time) AS first_ts,MAX(tick_time) AS last_ts FROM tick_logs GROUP BY symbol ORDER BY cnt DESC")
            rows = cur.fetchall()
            for r in rows: r["first_ts"]=int(r["first_ts"]); r["last_ts"]=int(r["last_ts"])
        conn.close()
        return jsonify({"symbols":rows,"total_symbols":len(rows)})
    except Exception as e:
        return jsonify({"error":str(e)}),500

@app.route("/api/debug/db", methods=["GET"])
def api_debug_db():
    if not HAS_MYSQL or not _db_ok:
        with _buf_lock: bs = len(_write_buf)
        return jsonify({"db_ok":False,"has_mysql":HAS_MYSQL,"buffer_pending":bs,"host":MYSQL_HOST})
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total FROM tick_logs")
            total = cur.fetchone()["total"]
        conn.close()
        with _buf_lock: bs = len(_write_buf)
        return jsonify({"db_ok":True,"total_rows":total,"buffer_pending":bs})
    except Exception as e:
        return jsonify({"db_ok":False,"error":str(e)})

# ==================== 实时内存报价接口 ====================
@app.route("/api/quote-live", methods=["GET"])
def api_quote_live():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    norm = normalize_symbol(symbol)
    if not norm:
        return jsonify({"error": f"unknown symbol: {symbol}"}), 400
    with quote_cache_lock:
        q = latest_quote_cache.get(norm)
    if not q:
        return jsonify({"error": f"no data for {norm}"}), 404
    payload = build_live_quote_payload(dict(q))
    if not payload:
        return jsonify({"error": f"invalid data for {norm}"}), 500
    return jsonify(payload)

@app.route("/api/quotes-live", methods=["GET"])
def api_quotes_live():
    with quote_cache_lock:
        cache_items = list(latest_quote_cache.items())
    server_time = now_shanghai_str()
    data = []
    for sym, q in cache_items:
        p = build_live_quote_payload(dict(q))
        if p:
            data.append(p)
    data.sort(key=lambda x: x["symbol"])
    return jsonify({"count": len(data), "server_time_shanghai": server_time, "data": data})

# ==================== 数据库报价接口 ====================
@app.route("/api/quote", methods=["GET"])
def api_quote():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    norm = normalize_symbol(symbol)
    if not norm:
        return jsonify({"error": f"unknown symbol: {symbol}"}), 400
    if not HAS_MYSQL:
        return jsonify({"error": "MySQL not available"}), 503
    server_time = now_shanghai_str()
    server_ts = int(now_shanghai_dt().timestamp() * 1000)
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol,bid,ask,spread,received_at FROM latest_quotes WHERE symbol=%s",
                (norm,))
            row = cur.fetchone()
        conn.close()
        if not row:
            return jsonify({"error": f"no data for {norm}"}), 404
        result = {"symbol": row["symbol"], "bid": row["bid"], "ask": row["ask"],
                  "spread": row["spread"],
                  "received_at": (row["received_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                                  if hasattr(row["received_at"], "strftime") else str(row["received_at"] or "")),
                  "quote_ts_ms": None, "quote_time_shanghai": "",
                  "server_ts_ms": server_ts, "server_time_shanghai": server_time,
                  "latency_ms": None, "is_stale": None}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotes", methods=["GET"])
def api_quotes():
    if not HAS_MYSQL:
        return jsonify({"error": "MySQL not available"}), 503
    server_time = now_shanghai_str()
    server_ts = int(now_shanghai_dt().timestamp() * 1000)
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT symbol,bid,ask,spread,received_at FROM latest_quotes ORDER BY symbol")
            rows = cur.fetchall()
        conn.close()
        formatted = []
        for r in rows:
            item = {"symbol": r["symbol"], "bid": r["bid"], "ask": r["ask"],
                    "spread": r["spread"],
                    "received_at": (r["received_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                                    if hasattr(r["received_at"], "strftime") else str(r.get("received_at") or "")),
                    "quote_ts_ms": None, "quote_time_shanghai": "",
                    "server_ts_ms": server_ts, "server_time_shanghai": server_time,
                    "latency_ms": None, "is_stale": None}
            formatted.append(item)
        return jsonify({"count": len(formatted), "server_time_shanghai": server_time, "data": formatted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quote-hist", methods=["GET"])
def api_quote_hist():
    if not HAS_MYSQL or not _db_ok:
        return jsonify({"error": "MySQL not available"}), 503
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    norm = normalize_symbol(symbol)
    limit = min(int(request.args.get("limit", 100)), 5000)
    now_sh = now_shanghai_dt()
    default_from = int(now_sh.timestamp() * 1000) - 3600000
    default_to = int(now_sh.timestamp() * 1000) + 60000
    from_ts = _parse_ts(request.args.get("from")) or default_from
    to_ts = _parse_ts(request.args.get("to")) or default_to
    from_str = ts_ms_to_db_str(from_ts)
    to_str = ts_ms_to_db_str(to_ts)
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol,bid,ask,spread,received_at FROM quote_ticks "
                "WHERE symbol=%s AND received_at>=%s AND received_at<=%s "
                "ORDER BY received_at ASC LIMIT %s",
                (norm, from_str, to_str, limit))
            rows = cur.fetchall()
        conn.close()
        formatted = []
        for r in rows:
            item = {"symbol": r["symbol"], "bid": r["bid"], "ask": r["ask"], "spread": r["spread"]}
            if hasattr(r.get("received_at"), "strftime"):
                item["received_at"] = r["received_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            else:
                item["received_at"] = str(r.get("received_at") or "")
            formatted.append(item)
        return jsonify({"symbol": norm, "from": from_ts, "to": to_ts,
                         "count": len(formatted), "data": formatted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug/quotes-db", methods=["GET"])
def api_debug_quotes_db():
    now_s = now_shanghai_str()
    now_ms = int(now_shanghai_dt().timestamp() * 1000)
    if not HAS_MYSQL:
        with _buf_lock: bs = len(_write_buf)
        return jsonify({"db_ok": False, "has_mysql": False,
                        "buffer_pending": bs, "server_time_shanghai": now_s, "server_ts_ms": now_ms})
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM latest_quotes")
            lq_cnt = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM quote_ticks")
            qt_cnt = cur.fetchone()["cnt"]
        conn.close()
        with _buf_lock: bs = len(_write_buf)
        return jsonify({"db_ok": _db_ok, "latest_quotes_count": lq_cnt,
                        "quote_ticks_count": qt_cnt, "buffer_pending": bs,
                        "server_time_shanghai": now_s, "server_ts_ms": now_ms})
    except Exception as e:
        return jsonify({"db_ok": False, "error": str(e),
                        "server_time_shanghai": now_s, "server_ts_ms": now_ms})

# ==================== SSE 持续监听接口 ====================
@app.route("/api/stream/quote", methods=["GET"])
def stream_quote():
    from flask import Response
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    norm = normalize_symbol(symbol)
    if not norm:
        return jsonify({"error": f"unknown symbol: {norm}"}), 400

    last_payload_str = ""
    heartbeat_counter = 0

    def generate():
        nonlocal last_payload_str, heartbeat_counter
        try:
            while True:
                with quote_cache_lock:
                    q = latest_quote_cache.get(norm)
                if q:
                    payload = build_live_quote_payload(dict(q))
                    if payload:
                        s = json.dumps(payload, ensure_ascii=False)
                        if s != last_payload_str:
                            last_payload_str = s
                            heartbeat_counter = 0
                            yield f"data: {s}\n\n"
                heartbeat_counter += 1
                if heartbeat_counter % 15 == 0:
                    yield ": ping\n\n"
                time.sleep(0.2)
        except GeneratorExit:
            pass
        except Exception as e:
            print(f"[SSE] stream_quote error: {repr(e)}")

    return Response(generate(), mimetype="text/event-stream",
                   headers={"X-Accel-Buffering": "no"})

@app.route("/api/stream/quotes", methods=["GET"])
def stream_quotes():
    from flask import Response
    limit_str = request.args.get("limit", "")
    limit_n = int(limit_str) if limit_str.isdigit() else None
    symbols_str = request.args.get("symbols", "")
    symbols_filter = set(symbols_str.upper().split(",")) if symbols_str else None

    last_payload_str = ""
    heartbeat_counter = 0

    def generate():
        nonlocal last_payload_str, heartbeat_counter
        try:
            while True:
                with quote_cache_lock:
                    items = list(latest_quote_cache.items())
                server_time = now_shanghai_str()
                data = []
                for sym, q in items:
                    if symbols_filter and sym not in symbols_filter:
                        continue
                    p = build_live_quote_payload(dict(q))
                    if p:
                        data.append(p)
                data.sort(key=lambda x: x["symbol"])
                if limit_n:
                    data = data[:limit_n]
                payload = {"count": len(data), "server_time_shanghai": server_time, "data": data}
                s = json.dumps(payload, ensure_ascii=False)
                if s != last_payload_str:
                    last_payload_str = s
                    heartbeat_counter = 0
                    yield f"data: {s}\n\n"
                heartbeat_counter += 1
                if heartbeat_counter % 30 == 0:
                    yield ": ping\n\n"
                time.sleep(0.5)
        except GeneratorExit:
            pass
        except Exception as e:
            print(f"[SSE] stream_quotes error: {repr(e)}")

    return Response(generate(), mimetype="text/event-stream",
                   headers={"X-Accel-Buffering": "no"})

@app.route("/api/health", methods=["GET"])
def api_health():
    with _tick_count_lock: cnt=_tick_count; last=_last_tick_at
    with quote_cache_lock: syms=list(latest_quote_cache.keys())
    with _buf_lock: bs=len(_write_buf)
    now_s = now_shanghai_str()
    now_ms = int(now_shanghai_dt().timestamp() * 1000)
    flush_age_ms = int((time.time() - _last_flush) * 1000)
    return jsonify({"status":"ok","ticks_received":cnt,"last_tick_at":last,
        "symbols_cached":len(syms),"symbols":syms[:20],"db_ok":_db_ok,"has_mysql":HAS_MYSQL,
        "server_time_shanghai":now_s,"server_ts_ms":now_ms,
        "buffer_pending":bs,"latest_flush_age_ms":flush_age_ms})

# ==================== K线 & 产品接口 ====================
@app.route("/api/kline", methods=["GET"])
def api_kline():
    symbol = normalize_symbol(request.args.get("symbol", "XAUUSD").upper())
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
:root.light{--bg:#f0f2f5;--card:#ffffff;--card2:#f5f5f5;--text:#1a1a1a;--muted:#666666;
--line:#d8dde6;--green:#0ea571;--red:#e53935;--yellow:#e6a700;--accent:#d4880a}
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
const _SH_OFF = 8 * 3600 * 1000;

function shTime(tsMs) {
  const d = new Date(tsMs + _SH_OFF);
  return {
    Y:  d.getUTCFullYear(),
    Mo: d.getUTCMonth() + 1,
    D:  d.getUTCDate(),
    h:  d.getUTCHours(),
    m:  d.getUTCMinutes(),
    s:  d.getUTCSeconds()
  };
}

function _p2(n) { return String(n).padStart(2, '0'); }

function fmtSH(tsMs, fmt) {
  const t = shTime(tsMs);
  if (fmt === 'HH:mm')     return `${_p2(t.h)}:${_p2(t.m)}`;
  if (fmt === 'M/D HH:mm') return `${t.Mo}/${_p2(t.D)} ${_p2(t.h)}:${_p2(t.m)}`;
  if (fmt === 'HH:mm:ss')  return `${_p2(t.h)}:${_p2(t.m)}:${_p2(t.s)}`;
  return `${t.Y}-${_p2(t.Mo)}-${_p2(t.D)} ${_p2(t.h)}:${_p2(t.m)}:${_p2(t.s)}`;
}

function applyThemeByShanghaiTime() {
  const h = shTime(Date.now()).h;
  if (h >= 20 || h < 6) {
    document.documentElement.classList.remove('light');
  } else {
    document.documentElement.classList.add('light');
  }
  if (cBars && cBars.length) drawK(); else drawE();
}
applyThemeByShanghaiTime();
setInterval(applyThemeByShanghaiTime, 60000);

const $=id=>document.getElementById(id);
const CP={
forex:["EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURGBP","EURJPY","GBPJPY","AUDJPY","EURAUD","EURCHF","GBPCHF","CHFJPY","CADJPY","AUDNZD","AUDCAD","AUDCHF","EURNZD","EURCAD","CADCHF","NZDJPY","GBPAUD","GBPCAD","GBPNZD","NZDCAD","NZDCHF","USDSGD","USDHKD","USDCNH"],
metal:["XAUUSD","XAGUSD"],commodity:["UKOUSD","USOUSD"],
index:["U30USD","NASUSD","SPXUSD","100GBP","D30EUR","E50EUR","H33HKD"],
crypto:["BTCUSD","ETHUSD","LTCUSD","BCHUSD","XMRUSD","BNBUSD","SOLUSD","ADAUSD","DOGUSD","XSIUSD","AVEUSD","DSHUSD","RPLUSD","LNKUSD"],
stock:["AAPL","AMZN","BABA","GOOGL","META","MSFT","NFLX","NVDA","TSLA","ABBV","ABNB","ABT","ADBE","AMD","AVGO","C","CRM","DIS","GS","INTC","JNJ","MA","MCD","KO","MMM","NIO","PLTR","SHOP","TSM","V"]
};
const CN={forex:"外汇",metal:"贵金属",commodity:"大宗商品",index:"指数",crypto:"虚拟货币",stock:"股票"};
let S="EURUSD",C="forex",TF="5min",lastM=null,stag=0,sigP=0;
let cBars=[];
let L=null;

let vStart=0;
let vBars=60;
const V_MIN=10, V_MAX=200;
let crosshair={active:false,x:0,y:0,barIdx:-1};

// ==================== ★ 修复5: SSE 连接管理 ====================
// ==================== 工具函数 ====================
function dg(s){if(!s)return 2;const u=s.toUpperCase();
if(u==="XAUUSD"||u==="XAGUSD"||u==="UKOUSD"||u==="USOUSD")return 2;
if(u==="BTCUSD"||u==="ETHUSD")return 2;
if(u.endsWith("JPY"))return 3;
if(["DOGUSD","ADAUSD","RPLUSD","XSIUSD","LNKUSD"].includes(u))return 5;
if(["LTCUSD","SOLUSD","BCHUSD","XMRUSD","BNBUSD","AVEUSD","DSHUSD"].includes(u))return 2;
if(["U30USD","NASUSD","SPXUSD","100GBP","D30EUR","E50EUR","H33HKD"].includes(u))return 1;
const fx=["USD","GBP","EUR","JPY","CHF","AUD","NZD","CAD","HKD","SGD","CNH"];
if(!fx.some(c=>u.includes(c)))return 2;
return 5;}
function fm(n,d){return n!=null?parseFloat(n).toFixed(d):'--'}
}

function nS(lo,hi,mt){
if(lo===hi){const d=lo===0?1:Math.abs(lo)*.01;lo-=d;hi+=d}
const r=hi-lo,p=r*.08;let mn=lo-p,mx=hi+p;
const rs=(mx-mn)/(mt-1),mg=Math.pow(10,Math.floor(Math.log10(rs))),res=rs/mg;
let ns;if(res<=1)ns=mg;else if(res<=2)ns=2*mg;else if(res<=2.5)ns=2.5*mg;
else if(res<=5)ns=5*mg;else ns=10*mg;
const tI=Math.floor(mn/ns)*ns,tA=Math.ceil(mx/ns)*ns,tk=[];
for(let v=tI;v<=tA+ns*.5;v+=ns)tk.push(v);
return{tI:tI,tA:tA,ns:ns,tk:tk}}

function drawK(){
const cv=$('kc');if(!cv)return;
const ctx=cv.getContext('2d');
const dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
if(w<=0||h<=0)return;
cv.width=w*dpr;cv.height=h*dpr;ctx.scale(dpr,dpr);
ctx.clearRect(0,0,w,h);

if(!cBars||cBars.length===0){drawE();return}

const PL=8,PR=76,PT=16,PB=32,cW=w-PL-PR,cH=h-PT-PB;
if(cW<=0||cH<=0)return;

const isLight = document.documentElement.classList.contains('light');
const G  = isLight ? '#0ea571' : '#0ecb81';
const R  = isLight ? '#e53935' : '#f6465d';
const gr = isLight ? 'rgba(0,0,0,0.07)' : 'rgba(255,255,255,0.04)';
const ax = isLight ? '#555555' : '#5e6673';

const totalBars=cBars.length;
const endIdx=Math.min(vStart+vBars,totalBars);
const startIdxF=vStart;
const endIdxF=Math.min(endIdx,totalBars);
const visibleCount=endIdxF-startIdxF;
if(visibleCount<=0){drawE();return}

const vb=cBars.slice(Math.floor(startIdxF),Math.ceil(endIdxF));
const n=vb.length;

let dMin=Infinity,dMax=-Infinity;
for(const b of vb){
  if(b[2]>dMax)dMax=b[2];
  if(b[3]<dMin)dMin=b[3];
}
const D=dg(S),sc=nS(dMin,dMax,6);
const yI=sc.tI,yA=sc.tA,yR=yA-yI||1;
const p2y=p=>PT+cH*(1-(p-yI)/yR),y2p=y=>yI+(1-(y-PT)/cH)*yR;

const bT=cW/visibleCount;
const gap=Math.max(1,Math.round(bT*.2));
const bW=Math.max(1,Math.floor(bT-gap));
const hB=bW/2;
const bxAt=(idx)=>PL+(idx-startIdxF+0.5)*bT;

L={PL,PR,PT,PB,cW,cH,bT,vBars,startIdxF,endIdxF,yI,yA,yR,p2y,y2p,D,vb,totalBars};

ctx.font='11px "SF Mono",Menlo,monospace';
ctx.textAlign='left';
ctx.textBaseline='middle';
for(const t of sc.tk){
  const y=p2y(t);
  if(y<PT-2||y>PT+cH+2)continue;
  ctx.strokeStyle=gr;ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(PL,Math.round(y)+.5);ctx.lineTo(PL+cW,Math.round(y)+.5);ctx.stroke();
  ctx.fillStyle=ax;ctx.fillText(t.toFixed(D),PL+cW+6,y)
}

for(let i=0;i<n;i++){
  const b=vb[i];
  const barIdx=Math.floor(startIdxF)+i;
  const x=bxAt(barIdx);
  const o=b[1],hi=b[2],lo=b[3],cl=b[4];
  const up=cl>=o,col=up?G:R;
  ctx.strokeStyle=col;ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(Math.round(x)+.5,Math.round(p2y(hi)));ctx.lineTo(Math.round(x)+.5,Math.round(p2y(lo)));ctx.stroke();
  const yO=p2y(o),yC=p2y(cl),bt=Math.min(yO,yC),bh=Math.abs(yC-yO);
  ctx.fillStyle=col;
  ctx.fillRect(Math.round(x-hB),Math.round(bt),Math.max(1,bW),Math.max(1,bh));
}

const lastBarIdx=Math.min(Math.floor(endIdxF)-1,totalBars-1);
if(lastBarIdx>=0){
  const lastBar=cBars[lastBarIdx];
  const lc=lastBar[4],up=lc>=lastBar[1],col=up?G:R,yL=p2y(lc);
  ctx.setLineDash([4,3]);ctx.strokeStyle=col;ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(PL,Math.round(yL)+.5);ctx.lineTo(PL+cW,Math.round(yL)+.5);ctx.stroke();ctx.setLineDash([]);
  const tW=PR-6,tH=18,tX=PL+cW+2,tY=Math.round(yL)-tH/2;
  ctx.fillStyle=col;ctx.beginPath();ctx.roundRect(tX,tY,tW,tH,3);ctx.fill();
  ctx.fillStyle='#fff';ctx.font='bold 11px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='middle';
  ctx.fillText(lc.toFixed(D),tX+tW/2,Math.round(yL));
}

ctx.fillStyle=ax;ctx.font='10px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='top';
const mG=60,iMs=TF==='1hour'?3600000*4:TF==='10min'?3600000:1800000;
let lX=-Infinity;
for(let i=0;i<visibleCount;i++){
  const barIdx=Math.floor(startIdxF)+i;
  if(barIdx<0||barIdx>=totalBars)continue;
  const ts=cBars[barIdx][0],x=bxAt(barIdx);
  if(barIdx!==0&&barIdx!==totalBars-1&&ts%iMs!==0)continue;
  if(x-lX<mG&&barIdx!==0&&barIdx!==totalBars-1)continue;
  const lb = TF==='1hour' ? fmtSH(ts,'M/D HH:mm') : fmtSH(ts,'HH:mm');
  ctx.strokeStyle=gr;ctx.lineWidth=1;
  ctx.beginPath();ctx.moveTo(Math.round(x)+.5,PT+cH);ctx.lineTo(Math.round(x)+.5,PT+cH+4);ctx.stroke();
  ctx.fillStyle=ax;ctx.fillText(lb,x,PT+cH+6);lX=x;
}

if(isDragging){
  const pct=(vStart/(totalBars-vBars))*100;
  const barPct=vBars/totalBars*100;
  drawScrollBar(pct,barPct);
}

drawCrosshair();
}

function drawScrollBar(posPct,barPct){
const cv=$('kc');if(!cv)return;
const ctx=cv.getContext('2d');
const dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
const H=4,y=h-H-4;
const barW=w*barPct/100;
const thumbX=w*posPct/100;
ctx.fillStyle='rgba(128,128,128,0.2)';
ctx.fillRect(0,y,w,H);
ctx.fillStyle='rgba(128,128,128,0.5)';
ctx.fillRect(Math.round(thumbX),y,Math.max(4,Math.round(barW)),H);
}

function drawE(){
const cv=$('kc');if(!cv)return;
const ctx=cv.getContext('2d'),dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
cv.width=w*dpr;cv.height=h*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
const isLight = document.documentElement.classList.contains('light');
ctx.fillStyle = isLight ? '#888888' : '#5e6673';
ctx.font='13px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText('暂无 K 线数据',w/2,h/2);
L=null;crosshair={active:false,x:0,y:0,barIdx:-1};
}

function drawCrosshair(){
const cv=$('cc');if(!cv)return;
const dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
cv.width=w*dpr;cv.height=h*dpr;cv.getContext('2d').scale(dpr,dpr);
if(!crosshair.active||!L){cv.getContext('2d').clearRect(0,0,w,h);return}
const ctx=cv.getContext('2d');
ctx.clearRect(0,0,w,h);

const isLight = document.documentElement.classList.contains('light');
const chCross = isLight ? 'rgba(0,0,0,0.2)'   : 'rgba(255,255,255,0.25)';
const chBg    = isLight ? '#c8cdd6'            : '#2b3139';
const chText  = isLight ? '#1a1a1a'            : '#eaecef';

const{PL,PT,cW,cH,bT,startIdxF,D,vb,totalBars}=L;
const cx=Math.max(PL,Math.min(crosshair.x,PL+cW)),cy=Math.max(PT,Math.min(crosshair.y,PT+cH));
const fracIdx=startIdxF+(cx-PL)/bT;
const idx=Math.max(0,Math.min(Math.floor(fracIdx),totalBars-1));

ctx.setLineDash([3,3]);ctx.strokeStyle=chCross;ctx.lineWidth=1;
ctx.beginPath();ctx.moveTo(Math.round(cx)+.5,PT);ctx.lineTo(Math.round(cx)+.5,PT+cH);ctx.stroke();
ctx.beginPath();ctx.moveTo(PL,Math.round(cy)+.5);ctx.lineTo(PL+cW,Math.round(cy)+.5);ctx.stroke();
ctx.setLineDash([]);

const pr=L.y2p(cy);
const tW2=64,tH2=18,tX2=PL+cW+2,tY2=Math.round(cy)-tH2/2;
ctx.fillStyle=chBg;
ctx.beginPath();ctx.roundRect(tX2,tY2,tW2,tH2,3);ctx.fill();
ctx.fillStyle=chText;ctx.font='11px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText(pr.toFixed(D),tX2+tW2/2,Math.round(cy));

if(idx>=0&&idx<totalBars){
  const b=cBars[idx],ts=b[0];
  const lb = TF==='1hour' ? fmtSH(ts,'M/D HH:mm') : fmtSH(ts,'HH:mm');
  const x=bxAtGlobal(idx);
  const tw=ctx.measureText(lb).width+12,tx=Math.round(x)-tw/2,ty=PT+cH+2;
  ctx.fillStyle=chBg;ctx.beginPath();ctx.roundRect(tx,ty,tw,16,3);ctx.fill();
  ctx.fillStyle=chText;ctx.font='10px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='top';
  ctx.fillText(lb,Math.round(x),ty+2);
  const o=b[1],hi=b[2],lo=b[3],cl=b[4],up=cl>=o;
  const isLightInner = document.documentElement.classList.contains('light');
  const c2=up?(isLightInner?'#0ea571':'#0ecb81'):(isLightInner?'#e53935':'#f6465d');
  $('oI').innerHTML=`<span style="color:${c2}">O:${o.toFixed(D)} H:${hi.toFixed(D)} L:${lo.toFixed(D)} C:${cl.toFixed(D)}</span>`
}else{
  $('oI').innerText='';
}}

function bxAtGlobal(idx){
if(!L)return 0;
return L.PL+(idx-L.startIdxF+0.5)*L.bT;
}

let isDragging=false,lastDragX=0;

function onDragStart(x){
isDragging=true;lastDragX=x;crosshair.active=false;drawK();
}
function onDragMove(x){
if(!isDragging)return;
const dx=lastDragX-x;
if(!L||!L.bT||cBars.length===0)return;
const totalBars=cBars.length;
const scrollable=totalBars-vBars;
if(scrollable<=0)return;
const idxDelta=dx/L.bT;
vStart=Math.max(0,Math.min(vStart+idxDelta,scrollable));
lastDragX=x;drawK();
}
function onDragEnd(){
isDragging=false;drawK();
}

function onWheel(e){
e.preventDefault();
if(!L||cBars.length===0)return;
const delta=e.deltaY<0?1:-1;
const factor=delta>0?0.85:1.18;
const newVBars=Math.max(V_MIN,Math.min(V_MAX,Math.round(vBars*factor)));
if(newVBars===vBars)return;
const mouseX=e.offsetX;
const frac=(mouseX-L.PL)/L.cW;
const mouseIdx=vStart+frac*vBars;
vBars=newVBars;
const scrollable=cBars.length-vBars;
if(scrollable>0){
  vStart=Math.max(0,Math.min(mouseIdx-frac*vBars,scrollable));
}else{
  vStart=0;
}
drawK();
}

let lastPinchDist=0;
function onPinchStart(touches){
lastPinchDist=Math.hypot(touches[0].clientX-touches[1].clientX,touches[0].clientY-touches[1].clientY);
}
function onPinchMove(touches){
const dist=Math.hypot(touches[0].clientX-touches[1].clientX,touches[0].clientY-touches[1].clientY);
if(!lastPinchDist)return;
const factor=dist/lastPinchDist;
const newVBars=Math.max(V_MIN,Math.min(V_MAX,Math.round(vBars/factor)));
if(newVBars!==vBars){
  const midX=(touches[0].clientX+touches[1].clientX)/2;
  const wrap=$('cW');if(!wrap)return;
  const rect=wrap.getBoundingClientRect();
  const relX=midX-rect.left;
  if(L&&L.cW>0){
    const frac=relX/L.cW;
    const midIdx=vStart+frac*vBars;
    vBars=newVBars;
    const scrollable=cBars.length-vBars;
    vStart=scrollable>0?Math.max(0,Math.min(midIdx-frac*vBars,scrollable)):0;
  }else{
    vBars=newVBars;
  }
  lastPinchDist=dist;
  drawK();
}else{
  lastPinchDist=dist;
}}

(function(){
const wr=$('cW');if(!wr)return;
const cc=$('cc');if(cc){cc.style.pointerEvents='none';}
wr.addEventListener('mousedown',e=>{if(e.button===0){onDragStart(e.offsetX)}});
wr.addEventListener('mousemove',e=>{
  if(isDragging){onDragMove(e.offsetX)}
  else{crosshair.active=true;crosshair.x=e.offsetX;crosshair.y=e.offsetY;drawCrosshair();}
});
wr.addEventListener('mouseleave',()=>{
  if(isDragging){isDragging=false;drawK()}
  crosshair.active=false;drawCrosshair();
});
wr.addEventListener('mouseup',()=>{if(isDragging)onDragEnd()});
wr.addEventListener('wheel',onWheel,{passive:false});
wr.addEventListener('touchstart',e=>{
  if(e.touches.length===1){onDragStart(e.touches[0].clientX-wr.getBoundingClientRect().left)}
  else if(e.touches.length===2){onPinchStart(e.touches)}
  e.preventDefault();
},{passive:false});
wr.addEventListener('touchmove',e=>{
  if(e.touches.length===1&&isDragging){onDragMove(e.touches[0].clientX-wr.getBoundingClientRect().left)}
  else if(e.touches.length===2){onPinchMove(e.touches)}
  e.preventDefault();
},{passive:false});
wr.addEventListener('touchend',e=>{
  if(isDragging)onDragEnd();
  lastPinchDist=0;
  e.preventDefault();
},{passive:false});
})();

function tick(){
  $('clk').innerText = fmtSH(Date.now(), 'HH:mm:ss');
}
function stab(b,c){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));b.classList.add('on');C=c}
function openM(){$('mTi').innerText='切换'+(CN[C]||'品种');
$('mLi').innerHTML=(CP[C]||[]).map(s=>`<div class="it ${s===S?'on':''}" onclick="pick('${s}')"><span>${s}</span></div>`).join('');
$('msk').style.display='flex'}
function pick(s){
  S=s;C=detectCategoryBySymbol(s);
  $('sN').innerText=s;$('msk').style.display='none';
  ['bP','aP','mP','mP2','sV','bB','bA'].forEach(id=>{if($(id))$(id).innerText='--'});
  $('sT').innerText='点差: --';lastM=null;stag=0;
  drawE();fK();
  vStart=0;vBars=60;
}
function detectCategoryBySymbol(sym){
for(const[c,arr]of Object.entries(CP)){if(arr.includes(sym))return c}
return'forex'}

// ★ 核心修复: rf() 每 2 秒轮询报价，用 AbortController 防止请求 hang
async function rf(){
  try{
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 5000);
    const r = await fetch('/api/latest_status?symbol='+encodeURIComponent(S), {signal: ctrl.signal});
    clearTimeout(timer);
    if(!r.ok){ cOk(false); return; }
    const d = await r.json();
    cOk(true);
    if(d.latest_quote){
      const q = d.latest_quote, D = dg(S);
      const b = Number(q.bid), a = Number(q.ask);
      if(Number.isFinite(b)&&Number.isFinite(a)){
        const mid = (b+a)/2, prev = lastM; lastM = mid;
        $('bP').innerText = fm(b,D); $('aP').innerText = fm(a,D);
        $('mP').innerText = fm(mid,D); $('mP2').innerText = fm(mid,D);
        $('bB').innerText = fm(b,D); $('bA').innerText = fm(a,D);
        const pe = $('mP'); pe.classList.remove('up','down');
        if(prev!=null){ if(mid>prev)pe.classList.add('up'); else if(mid<prev)pe.classList.add('down'); }
        const sp = q.spread!=null?q.spread:((a-b)*Math.pow(10,D>2?0:(5-D))).toFixed(1);
        $('sV').innerText = sp; $('sT').innerText='点差: '+sp;
      }
      if(q.latency_ms!=null&&Number.isFinite(q.latency_ms)){
        const lat=Math.round(q.latency_ms);
        if(Math.abs(lat)<=600000){$('lT').innerText='延迟: '+lat+'ms';lSig(lat)}
        else{$('lT').innerText='延迟: --';lSig(99999)}
      }else if(q.quote_time_shanghai){
        const t=q.quote_time_shanghai.split(' ')[1]||q.quote_time_shanghai;
        $('lT').innerText='更新: '+t;lSig(99999);
      }else{$('lT').innerText='延迟: --';lSig(99999);}
      if(q.quote_time_shanghai){ $('uT').innerText='数据: '+q.quote_time_shanghai; }
      else if(q.server_time_shanghai){ $('uT').innerText='数据: '+q.server_time_shanghai; }
      else if(q.received_at){ const t=q.received_at.split(' ')[1]||q.received_at; $('uT').innerText='更新: '+t; }
      else{$('uT').innerText='数据: --';}
    }else{
      // 服务器可达但无数据
      $('cT').innerText='已连接(无数据)';
      $('uT').innerText='等待 MT4 推送...';
    }
  }catch(e){
    console.error('[rf]', e.name||e, e.message||'');
    cOk(false);
    if(e.name==='AbortError') $('cT').innerText='请求超时';
    else $('cT').innerText='连接失败: '+(e.message||'');
  }
}

async function fK(){
try{
const maxLimit=TF==='5min'?300:TF==='10min'?250:200;
const r=await fetch('/api/kline?symbol='+encodeURIComponent(S)+'&tf='+encodeURIComponent(TF)+'&limit='+maxLimit),d=await r.json();
if(d.bars&&d.bars.length>0){
  cBars=d.bars;
  const scrollable=Math.max(0,cBars.length-vBars);
  vStart=Math.min(vStart,scrollable);
  drawK();
}else{cBars=[];drawE()}
}catch(e){cBars=[];drawE()}
}

function stf(b){document.querySelectorAll('.tf').forEach(t=>t.classList.remove('on'));b.classList.add('on');TF=b.dataset.tf;
vStart=0;vBars=60;fK()}

function lSig(v){const s=$('lS');if(!s)return;s.className='sg';
if(v<500)s.classList.add('g');else if(v<2000)s.classList.add('y');else s.classList.add('r')}
function cOk(ok){const d=$('cD'),t=$('cT');if(!d||!t)return;d.className='dot';
if(ok){d.classList.add('ok');t.innerText='已连接'}else{d.classList.add('err');t.innerText='连接断开'}}
function chkS(){const p=parseFloat($('mP')?.innerText)||0;
if(sigP===p&&p>0){stag++;if(stag>=5){const s=$('lS');if(s){s.className='sg';s.classList.add('r')}$('lT').innerText='数据停滞!'}}
else stag=0;sigP=p}

let lastKlineLen=0;
setInterval(async()=>{
try{
const maxLimit=TF==='5min'?300:TF==='10min'?250:200;
const r=await fetch('/api/kline?symbol='+encodeURIComponent(S)+'&tf='+encodeURIComponent(TF)+'&limit='+maxLimit);
if(!r.ok)return;
const d=await r.json();
if(!d.bars||!d.bars.length)return;
const newBars=d.bars;
if(newBars.length===lastKlineLen)return;
const wasAtEnd=(cBars.length===0)||(Math.abs((vStart+vBars)-cBars.length)<2);
cBars=newBars;lastKlineLen=cBars.length;
const scrollable=Math.max(0,cBars.length-vBars);
vStart=wasAtEnd?scrollable:Math.min(vStart,scrollable);
drawK();
}catch(e){}
},2000);

setInterval(chkS,2000);
tick();setInterval(tick,1000);
addEventListener('resize',()=>{clearTimeout(window._rt);window._rt=setTimeout(()=>{if(cBars.length)drawK();else drawE()},100)});

rf();fK();
setInterval(rf, 2000);
</script>
</body></html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/ping", methods=["GET"])
def api_ping():
    """最简连通性测试"""
    return jsonify({"pong": True, "time": now_shanghai_str()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug_mode, threaded=True)
