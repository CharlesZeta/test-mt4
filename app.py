"""
原始数据中转 API 服务
========================
只做三件事：
1. 接收原始 WebRequest，保留 body_raw，不做业务解析
2. 尝试基础 JSON 解析，结果存入 body_json / parse_ok / parse_error
3. 通过查询 API 将原始数据返还给调用方

所有旧 MT4 接口（/web/api/mt4/*、/api/tick）均作为兼容入口，
内部统一走 raw_store() 入库。
"""

import os
import json
import traceback
from datetime import datetime
from contextlib import contextmanager

from flask import Flask, request, jsonify

app = Flask(__name__)

# ============================================================
# 一、数据库层
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
    """每次查询自动借还连接，异常时回滚。"""
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
    """
    将一条原始请求记录写入 PostgreSQL（raw_requests 表）。
    返回新记录的 id（int）或 None（无持久化时）。
    """
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
        "source":       source,
        "category":     category,
        "method":       method,
        "path":        path,
        "client_ip":   client_ip,
        "headers_json": headers_json,
        "body_raw":     raw_body,
        "body_json":    body_json_str,
        "parse_ok":     parse_ok,
        "parse_error":  parse_error,
        "tick_time":    tick_time,
        "account":      account,
        "server":      server,
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
    """
    查询最新原始记录。
    filter 参数为 None 时不限制该字段。
    返回列表，每条为 dict（不含 body_raw，单独接口按需查）。
    """
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
    """
    分页查询原始记录历史。
    返回 (records, total_count)。
    """
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
    """按 id 查询完整原始记录（含 body_raw）。"""
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
    """返回 raw_requests 表当前总行数。"""
    with _db() as conn:
        if conn is None:
            return -1
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM raw_requests")
            return int(cur.fetchone()[0])


# ============================================================
# 二、通用工具
# ============================================================

def get_client_ip():
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or ""
    ).strip()

def try_parse_json(raw_body):
    """
    尝试将 body 解析为 JSON。
    返回 (body_json_dict 或 None, parse_error_str 或 None)。
    服务端不做任何业务字段提取。
    """
    if not raw_body or not isinstance(raw_body, str) and not isinstance(raw_body, bytes):
        return None, "empty body"
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="replace")
    raw_body = raw_body.strip()
    if not raw_body:
        return None, "empty body"
    try:
        parsed = json.loads(raw_body)
        return parsed, None
    except json.JSONDecodeError as e:
        return None, str(e)

def _map_path_to_category(path):
    """根据请求路径返回 category 标签。不做业务解析。"""
    p = path.rstrip("/")
    if p.endswith("/web/api/mt4/status"):    return "status"
    if p.endswith("/web/api/mt4/positions"): return "positions"
    if p.endswith("/web/api/mt4/report"):    return "report"
    if p.endswith("/web/api/mt4/quote"):      return "quote"
    if p.endswith("/web/api/mt4/commands"):   return "commands"
    if p.endswith("/web/api/echo"):          return "echo"
    if p.endswith("/api/tick"):              return "tick"
    if p.endswith("/ingest/raw"):            return "ingest"
    return "other"

def _extract_meta_from_json(body_json):
    """
    仅提取最外层可直接读取的元信息。
    不做字段存在性保证，不做类型假设，不做业务解释。
    """
    account = None
    server = None
    tick_time = None
    if isinstance(body_json, dict):
        account  = str(body_json["account"])  if "account"  in body_json else None
        server   = str(body_json["server"])   if "server"   in body_json else None
        tt       = body_json.get("tick_time") or body_json.get("ts") \
                   or body_json.get("time")   or body_json.get("Time")
        if tt is not None:
            try:
                tick_time = int(float(tt))
            except (TypeError, ValueError):
                tick_time = None
    return account, server, tick_time


# ============================================================
# 三、核心存储函数（统一入口）
# ============================================================

def raw_store(source="mt4"):
    """
    所有接收接口的统一存储函数。
    读取请求，解析 body，存入 DB，返回响应。
    不理解任何业务字段。
    """
    raw_body   = request.get_data(as_text=True) or ""
    method     = request.method
    path       = request.path
    client_ip  = get_client_ip()
    category   = _map_path_to_category(path)

    # 提取 headers 中有用的字段做记录（不做全量存储，降低膨胀）
    hdrs = {
        "Content-Type":     request.headers.get("Content-Type"),
        "X-Real-IP":        request.headers.get("X-Real-IP"),
        "X-Forwarded-For":  request.headers.get("X-Forwarded-For"),
        "User-Agent":       request.headers.get("User-Agent"),
        "Host":             request.headers.get("Host"),
    }
    headers_json = None
    try:
        headers_json = json.dumps({k: v for k, v in hdrs.items() if v}, ensure_ascii=False)
    except Exception:
        pass

    # 尝试基础 JSON 解析
    body_json, parse_error = try_parse_json(raw_body)

    # 若解析成功，提取元信息（可选，用于查询过滤）
    account, server, tick_time = None, None, None
    if body_json is not None:
        account, server, tick_time = _extract_meta_from_json(body_json)

    parse_ok = body_json is not None

    # 写入 DB
    new_id = db_insert(
        raw_body      = raw_body,
        headers_json  = headers_json,
        method        = method,
        path          = path,
        client_ip     = client_ip,
        source        = source,
        category      = category,
        parse_ok      = parse_ok,
        parse_error   = parse_error,
        tick_time     = tick_time,
        account       = account,
        server        = server,
    )
    return new_id, parse_ok, category


# ============================================================
# 四、接收接口
# ============================================================

@app.route("/ingest/raw", methods=["POST"])
def ingest_raw():
    """
    统一原始数据接收接口。
    MT4 或任意客户端均可直接 POST 任意 JSON 或原始 body。
    服务端只做：保留原始 body + 基础 JSON 解析 + 持久化。
    """
    new_id, parse_ok, category = raw_store(source="mt4")
    resp = {
        "ok":       True,
        "id":       new_id,
        "category": category,
        "parse_ok": parse_ok,
    }
    return jsonify(resp), 200


# 旧 MT4 兼容入口 —— 内部走统一 raw_store()
@app.route("/web/api/mt4/status",    methods=["POST"])
def mt4_status():
    raw_store(source="mt4")
    return "OK", 200

@app.route("/web/api/mt4/positions", methods=["POST"])
def mt4_positions():
    raw_store(source="mt4")
    return "OK", 200

@app.route("/web/api/mt4/report",    methods=["POST"])
def mt4_report():
    raw_store(source="mt4")
    return "OK", 200

@app.route("/web/api/mt4/quote",    methods=["POST"])
def mt4_quote():
    raw_store(source="mt4")
    return "OK", 200

@app.route("/web/api/echo",         methods=["POST"])
def mt4_echo():
    raw_store(source="mt4")
    return "OK", 200

@app.route("/web/api/mt4/commands", methods=["POST"])
def mt4_commands():
    raw_store(source="mt4")
    return jsonify({"commands": []}), 200

@app.route("/api/tick", methods=["POST"])
def api_tick():
    """保留 /api/tick 作为兼容入口，数据走统一存储。"""
    new_id, parse_ok, category = raw_store(source="mt4")
    return "", 204


# ============================================================
# 五、查询接口
# ============================================================

@app.route("/api/raw/latest", methods=["GET"])
def api_raw_latest():
    """
    获取最新一条原始记录。
    GET 参数:
      source   — 来源标识（默认 mt4）
      category — 分类标签（如 tick/status/positions/quote/report/other）
      path     — 请求路径（精确匹配）
      account  — 账户名（精确匹配）
      include_body — 是否返回 body_raw（默认 false，省流量）
    """
    source      = request.args.get("source",   "mt4")
    category     = request.args.get("category")
    path         = request.args.get("path")
    account      = request.args.get("account")
    include_body = request.args.get("include_body", "false").lower() in ("true", "1", "yes")

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
    if not include_body and "body_raw" not in row:
        row.pop("body_raw", None)

    return jsonify({"ok": True, "data": row, "total": 1})


@app.route("/api/raw/history", methods=["GET"])
def api_raw_history():
    """
    分页查询原始记录历史。
    GET 参数:
      source      — 来源标识（默认 mt4）
      category    — 分类标签
      path        — 请求路径
      account     — 账户名
      from_time   — ISO 格式起始时间（2019-01-01T00:00:00Z）
      to_time     — ISO 格式截止时间
      page        — 页码（默认 1）
      page_size   — 每页条数（默认 50，最大 500）
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
        page      = 1
        page_size = 50

    rows, total = db_query_history(
        source   = source,
        category = category,
        path     = path,
        account  = account,
        from_time = from_time,
        to_time   = to_time,
        page      = page,
        page_size = page_size,
    )

    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    return jsonify({
        "ok":          True,
        "data":        rows,
        "pagination": {
            "page":         page,
            "page_size":    page_size,
            "total":        total,
            "total_pages":  total_pages,
        }
    })


@app.route("/api/raw/by-id/<int:record_id>", methods=["GET"])
def api_raw_by_id(record_id):
    """
    根据 id 获取单条完整原始记录（含 body_raw）。
    """
    row = db_query_by_id(record_id)
    if row is None:
        return jsonify({"ok": False, "error": "record not found"}), 404
    return jsonify({"ok": True, "data": row})


@app.route("/api/raw/count", methods=["GET"])
def api_raw_count():
    """返回当前总记录数。"""
    return jsonify({"ok": True, "count": db_count()})


# ============================================================
# 六、健康检查
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
# 七、根路径 —— API 文档页
# ============================================================

API_DOC = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>MT4 原始数据中转 API</title>
<style>
*{box-sizing:border-box}body{font-family:-apple-system,system-ui,sans-serif;max-width:860px;margin:0 auto;padding:2rem;
color:#222;background:#f9f9f9;line-height:1.6}
h1{font-size:1.5rem;color:#111;border-bottom:2px solid #ddd;padding-bottom:.5rem}
h2{font-size:1.15rem;color:#333;margin-top:2rem}
.endpoint{margin:.75rem 0;padding:.875rem 1rem;background:#fff;border:1px solid #e0e0e0;border-radius:6px}
.method{display:inline-block;font-weight:700;font-size:.75rem;padding:.15rem .5rem;border-radius:4px;margin-right:.5rem}
.get{background:#61affe;color:#fff}.post{background:#49cc90;color:#fff}.delete{background:#f93e3e;color:#fff}
.path{font-family:monospace;font-size:.9rem;color:#333}
.desc{font-size:.85rem;color:#666;margin-top:.25rem}
.req{margin:.5rem 0 .25rem;font-weight:600;font-size:.8rem;color:#555}
.example{background:#f0f0f0;padding:.5rem .75rem;border-radius:4px;font-size:.8rem;font-family:monospace;overflow-x:auto}
table{width:100%;border-collapse:collapse;margin-top:.5rem;font-size:.85rem}
th,td{padding:.4rem .6rem;text-align:left;border-bottom:1px solid #eee}
th{color:#555;font-weight:600;background:#fafafa}
code{background:#eee;padding:.1rem .3rem;border-radius:3px;font-size:.85em}
</style>
</head>
<body>
<h1>MT4 原始数据中转 API</h1>
<p>服务端只做：接收原始请求 + 基础 JSON 解析 + 持久化存储。不解释任何业务字段。</p>

<h2>接收接口</h2>

<div class="endpoint">
  <span class="method post">POST</span><span class="path">/ingest/raw</span>
  <div class="desc">统一原始数据接收。MT4/EA 直接 POST 任意 JSON 或原始 body 即可。</div>
  <div class="req">响应示例</div>
  <div class="example">{"ok":true,"id":1234,"category":"tick","parse_ok":true}</div>
</div>

<div class="endpoint">
  <span class="method post">POST</span><span class="path">/web/api/mt4/status</span>
  <div class="desc">MT4 账户状态推送（兼容旧接口）</div>
</div>
<div class="endpoint">
  <span class="method post">POST</span><span class="path">/web/api/mt4/positions</span>
  <div class="desc">MT4 持仓推送（兼容旧接口）</div>
</div>
<div class="endpoint">
  <span class="method post">POST</span><span class="path">/web/api/mt4/report</span>
  <div class="desc">MT4 报告推送（兼容旧接口）</div>
</div>
<div class="endpoint">
  <span class="method post">POST</span><span class="path">/web/api/mt4/quote</span>
  <div class="desc">MT4 报价推送（兼容旧接口）</div>
</div>
<div class="endpoint">
  <span class="method post">POST</span><span class="path">/web/api/echo</span>
  <div class="desc">MT4 Echo（兼容旧接口）</div>
</div>
<div class="endpoint">
  <span class="method post">POST</span><span class="path">/api/tick</span>
  <div class="desc">Tick 数据推送（兼容旧接口）</div>
</div>

<h2>查询接口</h2>

<div class="endpoint">
  <span class="method get">GET</span><span class="path">/api/raw/latest</span>
  <div class="desc">获取最新一条原始记录</div>
  <div class="req">参数</div>
  <table>
    <tr><th>参数</th><th>说明</th></tr>
    <tr><td>source</td><td>来源标识，默认 mt4</td></tr>
    <tr><td>category</td><td>分类：tick / status / positions / quote / report / other</td></tr>
    <tr><td>path</td><td>请求路径（精确匹配）</td></tr>
    <tr><td>account</td><td>账户名（精确匹配）</td></tr>
    <tr><td>include_body</td><td>true=返回完整 body_raw（默认 false）</td></tr>
  </table>
</div>

<div class="endpoint">
  <span class="method get">GET</span><span class="path">/api/raw/history</span>
  <div class="desc">分页查询原始记录历史</div>
  <div class="req">参数</div>
  <table>
    <tr><th>参数</th><th>说明</th></tr>
    <tr><td>source</td><td>来源标识，默认 mt4</td></tr>
    <tr><td>category</td><td>分类标签</td></tr>
    <tr><td>path</td><td>请求路径</td></tr>
    <tr><td>account</td><td>账户名</td></tr>
    <tr><td>from_time</td><td>起始时间（ISO 格式）</td></tr>
    <tr><td>to_time</td><td>截止时间（ISO 格式）</td></tr>
    <tr><td>page</td><td>页码，默认 1</td></tr>
    <tr><td>page_size</td><td>每页条数，默认 50，最大 500</td></tr>
  </table>
</div>

<div class="endpoint">
  <span class="method get">GET</span><span class="path">/api/raw/by-id/&lt;id&gt;</span>
  <div class="desc">根据 id 获取单条完整原始记录（含 body_raw）</div>
</div>

<div class="endpoint">
  <span class="method get">GET</span><span class="path">/api/raw/count</span>
  <div class="desc">返回当前总记录数</div>
</div>

<div class="endpoint">
  <span class="method get">GET</span><span class="path">/api/v1/health</span>
  <div class="desc">健康检查</div>
</div>
</body>
</html>"""

@app.route("/")
def index():
    return API_DOC, 200, {"Content-Type": "text/html; charset=utf-8"}


# ============================================================
# 八、启动
# ============================================================

with app.app_context():
    _init_pool()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
