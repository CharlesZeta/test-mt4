"""
MT4 量化终端 — Flask 服务
========================
功能：
  - MT4 通过 HTTP POST 推送 status / positions / report / quote / tick
  - 内存中缓存最新行情、K 线、账户状态
  - 原始请求数据同步持久化到 PostgreSQL
  - 通过 /api/setting/* 提供原始数据查询接口
  - 页面展示行情和 K 线（纯 API 消费者）
"""

import os
import json
import time
import threading
import traceback
from datetime import datetime
from collections import deque
from contextlib import contextmanager

from flask import Flask, request, render_template_string, jsonify

app = Flask(__name__)

# ============================================================
# 第一部分：数据库层（原始数据持久化）
# ============================================================

def _get_db_url():
    return os.environ.get("DATABASE_URL", "").strip()

_db_pool = None

def _init_pool():
    global _db_pool
    if _db_pool is not None:
        return
    url = _get_db_url()
    if not url:
        print("[WARN] DATABASE_URL not set; running in no-persistence mode.")
        _db_pool = None
        return
    try:
        import psycopg2.pool
    except ImportError:
        print("[WARN] psycopg2 not installed; running in no-persistence mode.")
        _db_pool = None
        return
    _db_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1, maxconn=10,
        dsn=url,
        connect_timeout=5,
        options="-c statement_timeout=10000"
    )

@contextmanager
def _db():
    conn = None
    try:
        if _db_pool:
            conn = _db_pool.getconn()
            yield conn
            conn.commit()
        else:
            yield None
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn and _db_pool:
            _db_pool.putconn(conn)

def db_insert(raw_body, headers_json, method, path, client_ip,
              source, category, parse_ok, parse_error,
              tick_time, account, server):
    sql = """
        INSERT INTO raw_requests
            (received_at, source, category, method, path, client_ip,
             headers_json, body_raw, body_json, parse_ok, parse_error,
             tick_time, account, server)
        VALUES
            (NOW(), %(source)s, %(category)s, %(method)s, %(path)s, %(client_ip)s,
             %(headers_json)s, %(body_raw)s, %(body_json)s, %(parse_ok)s, %(parse_error)s,
             %(tick_time)s, %(account)s, %(server)s)
        RETURNING id
    """
    body_json_str = None
    if parse_ok and raw_body:
        try:
            body_json_str = json.dumps(json.loads(raw_body), ensure_ascii=False)
        except Exception:
            body_json_str = None
    params = {
        "source":        source,
        "category":      category,
        "method":        method,
        "path":          path,
        "client_ip":     client_ip,
        "headers_json":  headers_json,
        "body_raw":      raw_body,
        "body_json":     body_json_str,
        "parse_ok":      parse_ok,
        "parse_error":   parse_error,
        "tick_time":     tick_time,
        "account":       account,
        "server":        server,
    }
    with _db() as conn:
        if conn is None:
            return None
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else None

def db_query_latest(source=None, category=None, path=None,
                    account=None, limit=1):
    conditions = []
    params = {}
    if source:
        conditions.append("source = %(source)s")
        params["source"] = source
    if category:
        conditions.append("category = %(category)s")
        params["category"] = category
    if path:
        conditions.append("path = %(path)s")
        params["path"] = path
    if account:
        conditions.append("account = %(account)s")
        params["account"] = account
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params["limit"] = max(1, min(limit, 100))
    sql = f"""
        SELECT id, received_at, source, category, method, path,
               client_ip, headers_json, body_json, parse_ok, parse_error,
               tick_time, account, server
        FROM   raw_requests
        {where}
        ORDER BY received_at DESC
        LIMIT %(limit)s
    """
    with _db() as conn:
        if conn is None:
            return []
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]

def db_query_history(source=None, category=None, path=None,
                     account=None, from_time=None, to_time=None,
                     page=1, page_size=50):
    conditions = []
    params = {}
    if source:
        conditions.append("source = %(source)s")
        params["source"] = source
    if category:
        conditions.append("category = %(category)s")
        params["category"] = category
    if path:
        conditions.append("path = %(path)s")
        params["path"] = path
    if account:
        conditions.append("account = %(account)s")
        params["account"] = account
    if from_time:
        conditions.append("received_at >= %(from_time)s")
        params["from_time"] = from_time
    if to_time:
        conditions.append("received_at <= %(to_time)s")
        params["to_time"] = to_time
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    count_sql = f"SELECT COUNT(*) FROM raw_requests {where}"
    offset = max(0, (max(1, page) - 1)) * max(1, min(page_size, 500))
    params["offset"] = offset
    params["limit"] = max(1, min(page_size, 500))
    sel_sql = f"""
        SELECT id, received_at, source, category, method, path,
               client_ip, headers_json, body_json, parse_ok, parse_error,
               tick_time, account, server
        FROM   raw_requests
        {where}
        ORDER BY received_at DESC
        OFFSET %(offset)s LIMIT %(limit)s
    """
    with _db() as conn:
        if conn is None:
            return [], 0
        with conn.cursor() as cur:
            cur.execute(count_sql, params)
            total = cur.fetchone()[0] or 0
            cur.execute(sel_sql, params)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
            return [dict(zip(cols, r)) for r in rows], int(total)

def db_query_by_id(record_id):
    sql = """
        SELECT id, received_at, source, category, method, path,
               client_ip, headers_json, body_raw, body_json,
               parse_ok, parse_error, tick_time, account, server
        FROM   raw_requests
        WHERE  id = %(id)s
    """
    with _db() as conn:
        if conn is None:
            return None
        with conn.cursor() as cur:
            cur.execute(sql, {"id": record_id})
            cols = [c[0] for c in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else None

def db_count():
    with _db() as conn:
        if conn is None:
            return -1
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM raw_requests")
            return int(cur.fetchone()[0])


# ============================================================
# 第二部分：原始数据接收工具
# ============================================================

def get_client_ip():
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    ).strip()

def try_parse_json(raw_body):
    if not raw_body or (not isinstance(raw_body, str) and not isinstance(raw_body, bytes)):
        return None, "empty body"
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="replace")
    raw_body = raw_body.strip()
    if not raw_body:
        return None, "empty body"
    try:
        return json.loads(raw_body), None
    except json.JSONDecodeError as e:
        return None, str(e)

def _map_path_to_category(path):
    p = path.rstrip("/")
    if p.endswith("/web/api/mt4/status"):    return "status"
    if p.endswith("/web/api/mt4/positions"): return "positions"
    if p.endswith("/web/api/mt4/report"):    return "report"
    if p.endswith("/web/api/mt4/quote"):      return "quote"
    if p.endswith("/web/api/mt4/commands"):  return "commands"
    if p.endswith("/web/api/echo"):          return "echo"
    if p.endswith("/api/tick"):              return "tick"
    if p.endswith("/ingest/raw"):            return "ingest"
    return "other"

def _extract_meta_from_json(body_json):
    account = None
    server = None
    tick_time = None
    if isinstance(body_json, dict):
        account = str(body_json["account"]) if "account" in body_json else None
        server  = str(body_json["server"])  if "server"  in body_json else None
        tt = (body_json.get("tick_time") or body_json.get("ts")
              or body_json.get("time")     or body_json.get("Time"))
        if tt is not None:
            try:
                tick_time = int(float(tt))
            except (TypeError, ValueError):
                tick_time = None
    return account, server, tick_time

def raw_store(source="mt4"):
    """所有接收接口的统一存储：写 DB + 更新内存缓存。"""
    raw_body  = request.get_data(as_text=True) or ""
    method    = request.method
    path      = request.path
    client_ip = get_client_ip()
    category  = _map_path_to_category(path)

    hdrs = {
        "Content-Type":    request.headers.get("Content-Type"),
        "X-Real-IP":     request.headers.get("X-Real-IP"),
        "X-Forwarded-For": request.headers.get("X-Forwarded-For"),
        "User-Agent":    request.headers.get("User-Agent"),
        "Host":          request.headers.get("Host"),
    }
    headers_json = None
    try:
        headers_json = json.dumps({k: v for k, v in hdrs.items() if v}, ensure_ascii=False)
    except Exception:
        pass

    body_json, parse_error = try_parse_json(raw_body)
    account, server, tick_time = None, None, None
    if body_json is not None:
        account, server, tick_time = _extract_meta_from_json(body_json)
    parse_ok = body_json is not None

    db_insert(
        raw_body     = raw_body,
        headers_json = headers_json,
        method       = method,
        path         = path,
        client_ip    = client_ip,
        source       = source,
        category     = category,
        parse_ok     = parse_ok,
        parse_error  = parse_error,
        tick_time    = tick_time,
        account      = account,
        server       = server,
    )

    # 同时写内存（保留原有展示逻辑）
    _write_memory(category, method, path, body_json, raw_body, client_ip)
    return parse_ok, category

def _write_memory(category, method, path, body_json, raw_body, client_ip):
    """
    将原始数据写入内存队列（供页面展示用）。
    当 category 为 quote/report 时，还原 desc 标记，
    确保 api_latest_status 能正确提取报价。
    """
    record = {
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip":          client_ip,
        "method":      method,
        "path":        path,
        "body_raw":    raw_body,
        "parsed":      body_json,
    }

    # 还原旧逻辑需要的 desc 标记，让 api_latest_status 能正常工作
    if category in ("quote", "report") and isinstance(body_json, dict):
        if "desc" not in body_json:
            record["parsed"] = dict(body_json)
            record["parsed"]["desc"] = "QUOTE_DATA"

    with history_lock:
        queue = _category_to_deque(category)
        if queue:
            queue.appendleft(record)

def _category_to_deque(category):
    return {
        "status":    history_status,
        "positions": history_positions,
        "report":    history_report,
        "quote":     history_report,
        "poll":      history_poll,
        "echo":      history_echo,
    }.get(category)


# ============================================================
# 第三部分：内存数据（页面展示用）
# ============================================================

MAX_HISTORY = 50
KLINE_MAX   = {"5min": 300, "10min": 250, "1hour": 200}

kline_data  = {}
kline_locks = {}

history_status    = deque(maxlen=MAX_HISTORY)
history_positions = deque(maxlen=MAX_HISTORY)
history_report    = deque(maxlen=MAX_HISTORY)
history_poll      = deque(maxlen=MAX_HISTORY)
history_echo      = deque(maxlen=MAX_HISTORY)
history_lock      = threading.RLock()

latest_quote_cache = {}
quote_cache_lock   = threading.Lock()

KNOWN_SYMBOLS = set()
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
    "ABT": {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "ADBE":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "AMD": {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "AVGO":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "C":   {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "CRM":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "DIS": {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "GS":  {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "INTC":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "JNJ": {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "MA":  {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "MCD": {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "KO":  {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "MMM": {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "NIO": {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "PLTR":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "SHOP":{"size":100,"lev":10,"currency":"USD","type":"stock"},
    "TSM": {"size":100,"lev":10,"currency":"USD","type":"stock"},
    "V":   {"size":100,"lev":10,"currency":"USD","type":"stock"},
}
KNOWN_SYMBOLS = set(PRODUCT_SPECS.keys())

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
    if "." in s:
        candidate = s.split(".")[0]
        if candidate in KNOWN_SYMBOLS:
            return candidate
    if "_" in s:
        candidate = s.split("_")[0]
        if candidate in KNOWN_SYMBOLS:
            return candidate
    return s

def get_kline_lock(symbol):
    if symbol not in kline_locks:
        kline_locks[symbol] = threading.Lock()
    return kline_locks[symbol]

def _floor_ts(ts_ms, tf):
    step = {"5min": 300, "10min": 600, "1hour": 3600}.get(tf, 60) * 1000
    return (ts_ms // step) * step

def update_kline(symbol, bid, ask, tick_ts_ms):
    mid  = (bid + ask) / 2.0
    norm = normalize_symbol(symbol)
    tfs  = ["5min", "10min", "1hour"]
    with get_kline_lock(norm):
        if norm not in kline_data:
            kline_data[norm] = {tf: [] for tf in tfs}
        for tf in tfs:
            bar_ts = _floor_ts(tick_ts_ms, tf)
            bars   = kline_data[norm][tf]
            if bars and bars[-1][0] == bar_ts:
                bar = bars[-1]
                bar[2] = max(bar[2], mid)
                bar[3] = min(bar[3], mid)
                bar[4] = mid
            else:
                bars.append([bar_ts, mid, mid, mid, mid])
                if len(bars) > KLINE_MAX[tf]:
                    bars[:] = bars[-KLINE_MAX[tf]:]

def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

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
        "bid": b, "ask": a, "symbol": norm,
        "spread": spread, "ts": ts,
        "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

def _symbol_matches(record_symbol_raw, filter_symbol):
    if not filter_symbol:
        return True
    if not record_symbol_raw:
        return False
    return normalize_symbol(record_symbol_raw) == filter_symbol


# ============================================================
# 第四部分：MT4 数据接收接口
# ============================================================

@app.route("/web/api/mt4/status",    methods=["POST"])
def mt4_status():
    parse_ok, category = raw_store(source="mt4")
    return "OK", 200

@app.route("/web/api/mt4/positions", methods=["POST"])
def mt4_positions():
    parse_ok, category = raw_store(source="mt4")
    return "OK", 200

@app.route("/web/api/mt4/report",    methods=["POST"])
def mt4_report():
    parse_ok, category = raw_store(source="mt4")
    # 同时尝试从 body_json 提取报价并更新快速缓存
    raw_body = request.get_data(as_text=True) or ""
    body_json, _ = try_parse_json(raw_body)
    if isinstance(body_json, dict):
        ingest_quote_from_parsed(body_json)
    return "OK", 200

@app.route("/web/api/mt4/quote",      methods=["POST"])
def mt4_quote():
    parse_ok, category = raw_store(source="mt4")
    # 同时尝试从 body_json 提取报价并更新快速缓存
    raw_body = request.get_data(as_text=True) or ""
    body_json, _ = try_parse_json(raw_body)
    if isinstance(body_json, dict):
        ingest_quote_from_parsed(body_json)
    return "OK", 200

@app.route("/web/api/echo",          methods=["POST"])
def mt4_echo():
    parse_ok, category = raw_store(source="mt4")
    return "OK", 200

@app.route("/web/api/mt4/commands",  methods=["POST"])
def mt4_commands():
    raw_store(source="mt4")
    return jsonify({"commands": []}), 200

@app.route("/ingest/raw", methods=["POST"])
def ingest_raw():
    """统一原始数据接收入口。"""
    parse_ok, category = raw_store(source="mt4")
    return jsonify({"ok": True, "category": category, "parse_ok": parse_ok}), 200

@app.route("/api/tick", methods=["POST"])
def receive_tick():
    try:
        raw_body = request.get_data(as_text=True)
        method   = request.method
        path     = request.path
        client_ip = get_client_ip()
        category = "tick"

        body_json, parse_error = try_parse_json(raw_body)
        account, server, tick_time = None, None, None
        if body_json is not None:
            account, server, tick_time = _extract_meta_from_json(body_json)

        db_insert(
            raw_body     = raw_body,
            headers_json = None,
            method       = method,
            path         = path,
            client_ip    = client_ip,
            source       = "mt4",
            category     = category,
            parse_ok     = body_json is not None,
            parse_error  = parse_error,
            tick_time    = tick_time,
            account      = account,
            server       = server,
        )

        ticks = request.json
        if not isinstance(ticks, list):
            ticks = [ticks]
        for tick in ticks:
            sym_raw = tick.get("symbol")
            bid, ask = tick.get("bid"), tick.get("ask")
            tt = tick.get("tick_time")
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

            update_kline(sym_raw, bf, af, ts_ms)
            cache_tick_quote(sym_raw, bf, af, spread=tick.get("spread"), ts=tt)

            record = {
                "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ip": client_ip, "method": "POST", "path": "/api/tick",
                "body_raw": json.dumps(tick),
                "parsed": {"desc": "QUOTE_DATA", "spread": tick.get("spread", 0),
                           "ts": tt, "message": json.dumps({"bid": bf, "ask": af}),
                           "symbol": sym_raw, "account": "tick_stream"},
            }
            with history_lock:
                history_report.appendleft(record)

        return "", 204
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# 第五部分：页面展示用查询接口
# ============================================================

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

@app.route("/api/debug/queues", methods=["GET"])
def api_debug_queues():
    """调试接口：查看所有队列状态"""
    with history_lock:
        report_sample = []
        for r in list(history_report)[:5]:
            report_sample.append({
                "received_at": r.get("received_at"),
                "path": r.get("path"),
                "parsed_keys": list(r.get("parsed", {}).keys()) if isinstance(r.get("parsed"), dict) else str(type(r.get("parsed"))),
                "has_desc": isinstance(r.get("parsed"), dict) and "desc" in r.get("parsed"),
                "desc_value": r.get("parsed", {}).get("desc") if isinstance(r.get("parsed"), dict) else None,
            })
        status_sample = []
        for r in list(history_status)[:3]:
            status_sample.append({
                "received_at": r.get("received_at"),
                "path": r.get("path"),
                "parsed_keys": list(r.get("parsed", {}).keys()) if isinstance(r.get("parsed"), dict) else None,
            })
    with quote_cache_lock:
        quote_cache_snapshot = dict(latest_quote_cache)
    return jsonify({
        "history_report_count": len(history_report),
        "history_report_sample": report_sample,
        "history_status_count": len(history_status),
        "history_status_sample": status_sample,
        "kline_symbols": list(kline_data.keys()),
        "quote_cache_keys": list(quote_cache_snapshot.keys()),
        "quote_cache_sample": {k: v for k, v in list(quote_cache_snapshot.items())[:3]},
    })

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
                if not isinstance(p, dict):
                    continue
                if p.get("desc") == "QUOTE_DATA":
                    rs = p.get("symbol", "")
                    if sf and not _symbol_matches(rs, sf):
                        continue
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
                if not isinstance(p, dict):
                    continue
                rs = p.get("symbol", "")
                if sf and not _symbol_matches(rs, sf):
                    continue
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
                    if _symbol_matches(str(pos.get("symbol", "")), sf):
                        latest_quote = {"bid": None, "ask": None, "symbol": sf,
                            "spread": None, "ts": None, "received_at": sr.get("received_at")}
                        break

        if sr:
            pd = sr.get("parsed", {})
            detail = {k: pd.get(k) for k in ["account","server","balance","equity",
                      "margin","free_margin","margin_level","floating_pnl","leverage_used"]}
            detail["received_at"] = sr.get("received_at")
            detail["positions"] = pd.get("positions") or (pr.get("parsed", {}).get("positions") if pr else [])
        elif pr:
            pd = pr.get("parsed", {})
            detail = {"received_at": pr.get("received_at"), "account": pd.get("account"),
                      "server": pd.get("server"), "positions": pd.get("positions") or []}

    return jsonify({"detail": detail, "latest_quote": latest_quote})

@app.route("/api/kline", methods=["GET"])
def api_kline():
    symbol = request.args.get("symbol", "XAUUSD").upper()
    tf     = request.args.get("tf", "5min")
    if tf not in KLINE_MAX:
        tf = "5min"
    limit  = min(request.args.get("limit", KLINE_MAX[tf], type=int), KLINE_MAX[tf])
    with get_kline_lock(symbol):
        bars = list(kline_data.get(symbol, {}).get(tf, []))
    return jsonify({"symbol": symbol, "tf": tf, "bars": bars[-limit:]})

@app.route("/api/products", methods=["GET"])
def api_products():
    return jsonify(PRODUCT_SPECS)


# ============================================================
# 第六部分：原始数据查询接口 /api/setting/*
# ============================================================

@app.route("/api/setting/latest", methods=["GET"])
def api_setting_latest():
    """
    获取最新一条原始记录。
    GET 参数:
      source       — 来源标识（默认 mt4）
      category     — 分类标签（tick / status / positions / quote / report / other）
      path         — 请求路径（精确匹配）
      account      — 账户名（精确匹配）
      include_body — 是否返回 body_raw（默认 false）
    """
    source       = request.args.get("source",       "mt4")
    category      = request.args.get("category")
    path          = request.args.get("path")
    account       = request.args.get("account")
    include_body  = request.args.get("include_body", "false").lower() in ("true", "1", "yes")

    rows = db_query_latest(
        source   = source,
        category = category,
        path     = path,
        account  = account,
        limit    = 1,
    )
    if not rows:
        return jsonify({"ok": True, "data": None, "total": 0})
    row = rows[0]
    if not include_body:
        row.pop("body_raw", None)
    return jsonify({"ok": True, "data": row, "total": 1})

@app.route("/api/setting/history", methods=["GET"])
def api_setting_history():
    """
    分页查询原始记录历史。
    GET 参数:
      source     — 来源标识（默认 mt4）
      category   — 分类标签
      path       — 请求路径
      account    — 账户名
      from_time  — 起始时间（ISO 格式）
      to_time    — 截止时间（ISO 格式）
      page       — 页码（默认 1）
      page_size  — 每页条数（默认 50，最大 500）
    """
    source    = request.args.get("source",    "mt4")
    category  = request.args.get("category")
    path      = request.args.get("path")
    account   = request.args.get("account")
    from_time = request.args.get("from_time")
    to_time   = request.args.get("to_time")
    page      = request.args.get("page",      "1")
    page_size = request.args.get("page_size", "50")

    try:
        page      = max(1, int(page))
        page_size = max(1, min(500, int(page_size)))
    except (TypeError, ValueError):
        page, page_size = 1, 50

    rows, total = db_query_history(
        source    = source,
        category  = category,
        path      = path,
        account   = account,
        from_time = from_time,
        to_time   = to_time,
        page      = page,
        page_size = page_size,
    )
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    return jsonify({
        "ok": True,
        "data": rows,
        "pagination": {
            "page":        page,
            "page_size":   page_size,
            "total":       total,
            "total_pages": total_pages,
        }
    })

@app.route("/api/setting/by-id/<int:record_id>", methods=["GET"])
def api_setting_by_id(record_id):
    """根据 id 获取单条完整原始记录（含 body_raw）。"""
    row = db_query_by_id(record_id)
    if row is None:
        return jsonify({"ok": False, "error": "record not found"}), 404
    return jsonify({"ok": True, "data": row})

@app.route("/api/setting/count", methods=["GET"])
def api_setting_count():
    """返回当前总记录数。"""
    return jsonify({"ok": True, "count": db_count()})


# ============================================================
# 第七部分：健康检查
# ============================================================

@app.route("/api/v1/health", methods=["GET"])
def health():
    checks = {"ok": True, "checks": {}}
    try:
        with _db() as conn:
            if conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                checks["checks"]["db"] = "ok"
            else:
                checks["checks"]["db"] = "no-persistence"
                checks["ok"] = False
    except Exception as e:
        checks["checks"]["db"] = f"error: {e}"
        checks["ok"] = False
    return jsonify(checks), 200 if checks["ok"] else 503


# ============================================================
# 第八部分：前端页面
# ============================================================

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
<div class="sc"><span class="sn" id="sN">EURUSD</span><span class="sb" onclick="openM()">切换</span></div>
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
<div class="mdl"><div class="mh"><span id="mTi">切换品种</span><button class="xb" onclick="msk.style.display='none'">X</button></div>
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

function dg(s){
if(!s)return 2;const u=s.toUpperCase();
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
$mLi.innerHTML=(CP[C]||[]).map(s=>`<div class="it ${s===S?'on':''}" onclick="pick('${s}')"><span>${s}</span></div>`).join('');
$msk.style.display='flex'}
function pick(s){S=s;$sN.innerText=s;$msk.style.display='none';
['bP','aP','mP','mP2','sV','bB','bA'].forEach(id=>{if($(id))$(id).innerText='--'});
$sT.innerText='点差: --';lastM=null;stag=0;drawE();rf();fK()}

async function rf(){try{
const r=await fetch('/api/latest_status?symbol='+encodeURIComponent(S)),d=await r.json();
cOk(true);
if(d.latest_quote){const q=d.latest_quote,D=dg(S);
const b=Number(q.bid),a=Number(q.ask);
if(Number.isFinite(b)&&Number.isFinite(a)){
const mid=(b+a)/2,prev=lastM;lastM=mid;
$bP.innerText=fm(b,D);$aP.innerText=fm(a,D);
$mP.innerText=fm(mid,D);$mP2.innerText=fm(mid,D);
$bB.innerText=fm(b,D);$bA.innerText=fm(a,D);
const pe=$mP;pe.classList.remove('up','down');
if(prev!=null){if(mid>prev)pe.classList.add('up');else if(mid<prev)pe.classList.add('down')}
const sp=q.spread!=null?q.spread:((a-b)*Math.pow(10,D>2?0:(5-D))).toFixed(1);
$sV.innerText=sp;$sT.innerText='点差: '+sp}
if(q.ts!=null){const t=Number(q.ts),sMs=t<1e12?t*1000:t,now=Date.now(),lat=Math.round(now-sMs);
if(Number.isFinite(sMs)&&Number.isFinite(lat)&&Math.abs(lat)<=600000){$lT.innerText='延迟: '+lat+'ms';lSig(lat)}
else{$lT.innerText='延迟: --';lSig(99999)}
const dt=new Date(sMs);
$uT.innerText='数据: '+dt.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).replace(/\//g,'-')}
else if(q.received_at)$uT.innerText='更新: '+(q.received_at.split(' ')[1]||'--')
}}catch(e){console.error(e);cOk(false)}}

async function fK(){try{
const lm={_5min:300,_10min:250,_1hour:200};
const r=await fetch('/api/kline?symbol='+encodeURIComponent(S)+'&tf='+encodeURIComponent(TF)+'&limit='+(lm['_'+TF]||300)),d=await r.json();
if(d.bars&&d.bars.length>0){cBars=d.bars;drawK(d.bars)}else{cBars=[];drawE()}
}catch(e){drawE()}}
function stf(b){document.querySelectorAll('.tf').forEach(t=>t.classList.remove('on'));b.classList.add('on');TF=b.dataset.tf;fK()}

function drawK(bars){
const cv=$kc;if(!cv)return;const ctx=cv.getContext('2d');
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
ctx.font='11px "SF Mono",Menlo,monospace';ctx.textAlign='left';ctx.textBaseline='middle';
for(const t of sc.tk){const y=p2y(t);if(y<PT-2||y>PT+cH+2)continue;
ctx.strokeStyle=gr;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(PL,Math.round(y)+.5);ctx.lineTo(PL+cW,Math.round(y)+.5);ctx.stroke();
ctx.fillStyle=ax;ctx.fillText(t.toFixed(D),PL+cW+8,y)}
for(let i=0;i<n;i++){const b=vb[i],o=b[1],hi=b[2],lo=b[3],cl=b[4];
const up=cl>=o,col=up?G:R,x=bx(i);
ctx.strokeStyle=col;ctx.lineWidth=1;ctx.beginPath();
ctx.moveTo(Math.round(x)+.5,Math.round(p2y(hi)));ctx.lineTo(Math.round(x)+.5,Math.round(p2y(lo)));ctx.stroke();
const yO=p2y(o),yC=p2y(cl),bt=Math.min(yO,yC),bb=Math.max(yO,yC);
ctx.fillStyle=col;ctx.fillRect(Math.round(x-hB),Math.round(bt),bW,Math.max(1,bb-bt))}
if(n>0){const lc=vb[n-1][4],up=lc>=vb[n-1][1],col=up?G:R,yL=p2y(lc);
ctx.setLineDash([4,3]);ctx.strokeStyle=col;ctx.lineWidth=1;
ctx.beginPath();ctx.moveTo(PL,Math.round(yL)+.5);ctx.lineTo(PL+cW,Math.round(yL)+.5);ctx.stroke();ctx.setLineDash([]);
const tW=PR-6,tH=18,tX=PL+cW+2,tY=Math.round(yL)-tH/2;
ctx.fillStyle=col;ctx.beginPath();ctx.roundRect(tX,tY,tW,tH,3);ctx.fill();
ctx.fillStyle='#fff';ctx.font='bold 11px "SF Mono",Menlo,monospace';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText(lc.toFixed(D),tX+tW/2,Math.round(yL))}
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

function drawE(){const cv=$kc;if(!cv)return;const ctx=cv.getContext('2d'),dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
cv.width=w*dpr;cv.height=h*dpr;ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
ctx.fillStyle='#5e6673';ctx.font='13px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
ctx.fillText('暂无 K 线数据',w/2,h/2);clrCH();L=null}

function clrCH(){const cv=$cc;if(!cv)return;const dpr=devicePixelRatio||1,w=cv.offsetWidth,h=cv.offsetHeight;
cv.width=w*dpr;cv.height=h*dpr;cv.getContext('2d').scale(dpr,dpr);$oI.innerText=''}
function dCH(mx,my){const cv=$cc;if(!cv||!L)return;
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
$oI.innerHTML=`<span style="color:${c2}">O:${o.toFixed(D)} H:${hi.toFixed(D)} L:${lo.toFixed(D)} C:${cl.toFixed(D)}</span>`}}

(function(){const wr=$cW;if(!wr)return;
function pos(e){const r=wr.getBoundingClientRect();return e.touches?{x:e.touches[0].clientX-r.left,y:e.touches[0].clientY-r.top}:{x:e.clientX-r.left,y:e.clientY-r.top}}
wr.addEventListener('mousemove',e=>{const p=pos(e);dCH(p.x,p.y)});
wr.addEventListener('mouseleave',()=>clrCH());
wr.addEventListener('touchmove',e=>{e.preventDefault();const p=pos(e);dCH(p.x,p.y)},{passive:false});
wr.addEventListener('touchend',()=>clrCH())})();

function lSig(v){const s=$lS;if(!s)return;s.className='sg';
if(v<500)s.classList.add('g');else if(v<2000)s.classList.add('y');else s.classList.add('r')}
function cOk(ok){const d=$cD,t=$cT;if(!d||!t)return;d.className='dot';
if(ok){d.classList.add('ok');t.innerText='已连接'}else{d.classList.add('err');t.innerText='连接断开'}}
function chkS(){const p=parseFloat($mP?.innerText)||0;
if(sigP===p&&p>0){stag++;if(stag>=5){const s=$lS;if(s){s.className='sg';s.classList.add('r')}$lT.innerText='数据停滞!'}}
else stag=0;sigP=p}

tick();setInterval(tick,1000);rf();setInterval(rf,2000);setInterval(chkS,2000);
fK();setInterval(fK,2000);
addEventListener('resize',()=>{clearTimeout(window._rt);window._rt=setTimeout(()=>{if(cBars.length)drawK(cBars);else drawE()},100)});
</script>
</body></html>"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


# ============================================================
# 启动
# ============================================================

with app.app_context():
    _init_pool()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
