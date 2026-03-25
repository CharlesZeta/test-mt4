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
            # 原有 tick_logs 表
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
            # ---- 新增：简化报价历史表（5字段模板）----
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
            # ---- 新增：最新报价快照表（每个 symbol 只保留一条）----
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

threading.Thread(target=init_db, daemon=True).start()

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

def _do_insert(batch):
    global _db_ok
    if not batch:
        return
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            # 1) 批量写入 quote_ticks（历史表）
            rows = [(r["symbol"], r["bid"], r["ask"], r["spread"], r["received_at"]) for r in batch]
            cur.executemany(
                "INSERT INTO quote_ticks (symbol,bid,ask,spread,received_at) "
                "VALUES (%s,%s,%s,%s,%s)", rows)
            # 2) Upsert 到 latest_quotes（最新报价快照表）
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
        print(f"[DB] insert failed (batch_size={len(batch)}): {repr(e)}")

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

# 与 K 线同源：最后一笔报价按归一化品种缓存，避免仅依赖 history 扫描或 JSON 形态差异导致页面不同步
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
    """
    生成统一5字段报价模板 dict。
    - 自动归一化 symbol
    - 对 bid/ask/spread 做安全 float 转换
    - received_at 使用东八区时间字符串
    - 无效数据返回 None
    """
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

def _bid_ask_from_dict(d):
    if not isinstance(d, dict):
        return None, None
    for bk, ak in (("bid", "ask"), ("Bid", "Ask"), ("BID", "ASK")):
        b, a = _to_float(d.get(bk)), _to_float(d.get(ak))
        if b is not None and a is not None:
            return b, a
    return None, None

def _message_to_quote_dict(p):
    """message 可能是 JSON 字符串或已是 dict"""
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
    """从 MT4 report 解析对象尽量提取 bid/ask 并写入缓存"""
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
        if tt is not None:
            tft = float(tt)
            ts_ms = int(tft) if tft > 1e12 else int(tft * 1000)
        else:
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

        # 3) MySQL buffer（5字段模板 dict）
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
    """Historical batch import: MySQL only, no memory update"""
    if not HAS_MYSQL:
        return jsonify({"ok": False, "error": "MySQL not available"}), 503
    try:
        ticks = request.json
        if not isinstance(ticks, list): ticks = [ticks]
        old_rows = []   # tick_logs 格式（保持兼容）
        new_rows = []   # quote_ticks / latest_quotes 格式
        for tick in ticks:
            sym_raw = tick.get('symbol')
            bid, ask = tick.get('bid'), tick.get('ask')
            tt = tick.get('tick_time')
            if not sym_raw or bid is None or ask is None: continue
            bf, af = _to_float(bid), _to_float(ask)
            if bf is None or af is None: continue
            spread = _to_float(tick.get('spread')) or 0
            if tt is not None:
                tft = float(tt)
                ts_ms = int(tft) if tft > 1e12 else int(tft * 1000)
            else:
                ts_ms = int(time.time() * 1000)
            mid = (bf + af) / 2.0
            norm = normalize_symbol(sym_raw)
            now_str = now_shanghai_str()
            # 兼容旧 tick_logs
            old_rows.append((norm, bf, af, spread, mid, ts_ms, now_str))
            # 写入新 5 字段表
            qr = make_quote_row(sym_raw, bid, ask, spread=spread)
            if qr:
                new_rows.append((qr["symbol"], qr["bid"], qr["ask"], qr["spread"], qr["received_at"]))
        conn = get_conn()
        with conn.cursor() as cur:
            if old_rows:
                cur.executemany(
                    "INSERT INTO tick_logs (symbol,bid,ask,spread,mid,tick_time,received_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)", old_rows)
            # 同时补充写入新表
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
        try: return int(datetime.strptime(val, fmt).timestamp() * 1000)
        except ValueError: continue
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
    from_ts = _parse_ts(request.args.get("from")) or (int(time.time()*1000) - 3600000)
    to_ts = _parse_ts(request.args.get("to")) or (int(time.time()*1000) + 60000)
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

# ==================== 简化报价查询（新 5 字段模板） ====================

@app.route("/api/quote", methods=["GET"])
def api_quote():
    """查询指定 symbol 的最新报价（从 latest_quotes 表）"""
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    norm = normalize_symbol(symbol)
    if not norm:
        return jsonify({"error": f"unknown symbol: {symbol}"}), 400
    if not HAS_MYSQL:
        return jsonify({"error": "MySQL not available"}), 503
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
        # 格式化 received_at
        result = dict(row)
        if isinstance(result.get("received_at"), datetime):
            result["received_at"] = result["received_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quotes", methods=["GET"])
def api_quotes():
    """返回所有产品的最新报价快照（从 latest_quotes 表）"""
    if not HAS_MYSQL:
        return jsonify({"error": "MySQL not available"}), 503
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT symbol,bid,ask,spread,received_at FROM latest_quotes ORDER BY symbol")
            rows = cur.fetchall()
        conn.close()
        formatted = []
        for r in rows:
            item = {"symbol": r["symbol"], "bid": r["bid"], "ask": r["ask"],
                    "spread": r["spread"]}
            if isinstance(r.get("received_at"), datetime):
                item["received_at"] = r["received_at"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            else:
                item["received_at"] = str(r.get("received_at") or "")
            formatted.append(item)
        return jsonify({"count": len(formatted), "data": formatted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/quote-hist", methods=["GET"])
def api_quote_hist():
    """查询 quote_ticks 历史，只返回 5 字段"""
    if not HAS_MYSQL or not _db_ok:
        return jsonify({"error": "MySQL not available"}), 503
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    norm = normalize_symbol(symbol)
    limit = min(int(request.args.get("limit", 100)), 5000)
    from_ts = _parse_ts(request.args.get("from")) or (int(time.time()*1000) - 3600000)
    to_ts = _parse_ts(request.args.get("to")) or (int(time.time()*1000) + 60000)
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol,bid,ask,spread,received_at FROM quote_ticks "
                "WHERE symbol=%s AND received_at>=FROM_UNIXTIME(%s/1000) "
                "AND received_at<=FROM_UNIXTIME(%s/1000) "
                "ORDER BY received_at ASC LIMIT %s",
                (norm, from_ts, to_ts, limit))
            rows = cur.fetchall()
        conn.close()
        formatted = []
        for r in rows:
            item = {"symbol": r["symbol"], "bid": r["bid"], "ask": r["ask"],
                    "spread": r["spread"]}
            if isinstance(r.get("received_at"), datetime):
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
    """调试接口：查看新报价表的统计信息"""
    if not HAS_MYSQL:
        with _buf_lock: bs = len(_write_buf)
        return jsonify({"db_ok": False, "has_mysql": False, "buffer_pending": bs})
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM latest_quotes")
            lq_cnt = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM quote_ticks")
            qt_cnt = cur.fetchone()["cnt"]
        conn.close()
        with _buf_lock: bs = len(_write_buf)
        return jsonify({
            "db_ok": _db_ok,
            "latest_quotes_count": lq_cnt,
            "quote_ticks_count": qt_cnt,
            "buffer_pending": bs,
        })
    except Exception as e:
        return jsonify({"db_ok": False, "error": str(e)})

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
            cur.execute("SELECT COUNT(*) AS lq_cnt FROM latest_quotes")
            lq_cnt = cur.fetchone()["lq_cnt"]
            cur.execute("SELECT COUNT(*) AS qt_cnt FROM quote_ticks")
            qt_cnt = cur.fetchone()["qt_cnt"]
        conn.close()
        with _buf_lock: bs = len(_write_buf)
        return jsonify({
            "db_ok": True, "total_rows": total,
            "latest_quotes_count": lq_cnt, "quote_ticks_count": qt_cnt,
            "buffer_pending": bs,
        })
    except Exception as e:
        return jsonify({"db_ok":False,"error":str(e)})

@app.route("/api/health", methods=["GET"])
def api_health():
    with _tick_count_lock: cnt=_tick_count; last=_last_tick_at
    with quote_cache_lock: syms=list(latest_quote_cache.keys())
    return jsonify({"status":"ok","ticks_received":cnt,"last_tick_at":last,
        "symbols_cached":len(syms),"symbols":syms[:20],"db_ok":_db_ok,"has_mysql":HAS_MYSQL})

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
const r=await fetch('/api/latest_status?symbol='+encodeURIComponent(S)),d=await r.json();
cOk(true);
if(d.latest_quote){const q=d.latest_quote,D=dg(S);
const b=Number(q.bid),a=Number(q.ask);
if(Number.isFinite(b)&&Number.isFinite(a)){
const mid=(b+a)/2,prev=lastM;lastM=mid;
$('bP').innerText=fm(b,D);$('aP').innerText=fm(a,D);
$('mP').innerText=fm(mid,D);$('mP2').innerText=fm(mid,D);
$('bB').innerText=fm(b,D);$('bA').innerText=fm(a,D);
const pe=$('mP');pe.classList.remove('up','down');
if(prev!=null){if(mid>prev)pe.classList.add('up');else if(mid<prev)pe.classList.add('down')}
const sp=q.spread!=null?q.spread:((a-b)*Math.pow(10,D>2?0:(5-D))).toFixed(1);
$('sV').innerText=sp;$('sT').innerText='点差: '+sp}
if(q.ts!=null){const t=Number(q.ts),sMs=t<1e12?t*1000:t,now=Date.now(),lat=Math.round(now-sMs);
if(Number.isFinite(sMs)&&Number.isFinite(lat)&&Math.abs(lat)<=600000){$('lT').innerText='延迟: '+lat+'ms';lSig(lat)}
else{$('lT').innerText='延迟: --';lSig(99999)}
const dt=new Date(sMs);
$('uT').innerText='数据: '+dt.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).replace(/\//g,'-')}
else if(q.received_at)$('uT').innerText='更新: '+(q.received_at.split(' ')[1]||'--')
}}catch(e){console.error(e);cOk(false)}}

async function fK(){try{
const lm={_5min:300,_10min:250,_1hour:200};
const r=await fetch('/api/kline?symbol='+encodeURIComponent(S)+'&tf='+encodeURIComponent(TF)+'&limit='+(lm['_'+TF]||300)),d=await r.json();
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
fK();setInterval(fK,2000);
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
