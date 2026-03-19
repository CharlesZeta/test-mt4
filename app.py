import os
import json
import threading
import traceback
import time
import random
import string
from datetime import datetime
from collections import deque
from flask import Flask, request, render_template_string, jsonify

app = Flask(__name__)

# ==================== 全局数据结构 ====================
MAX_HISTORY = 50

# 分类别存储，避免互相污染
history_status = deque(maxlen=MAX_HISTORY)     # /mt4/status
history_positions = deque(maxlen=MAX_HISTORY)  # /mt4/positions
history_report = deque(maxlen=MAX_HISTORY)     # /mt4/report
history_poll = deque(maxlen=MAX_HISTORY)       # /mt4/commands 轮询请求（account/max）
history_echo = deque(maxlen=MAX_HISTORY)        # /web/api/echo

history_lock = threading.RLock()

# ==================== 产品规则表 ====================
PRODUCT_SPECS = {
    # 1. 贵金属 / 原油
    "XAGUSD": {"size": 5000, "lev": 500, "currency": "USD", "type": "metal"},
    "XAUUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "metal"},
    "UKOUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "commodity"},
    "USOUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "commodity"},

    # 2. 指数
    "U30USD": {"size": 10, "lev": 100, "currency": "USD", "type": "index"},
    "NASUSD": {"size": 10, "lev": 100, "currency": "USD", "type": "index"},
    "SPXUSD": {"size": 100, "lev": 100, "currency": "USD", "type": "index"},
    "100GBP": {"size": 10, "lev": 100, "currency": "GBP", "type": "index"},
    "D30EUR": {"size": 10, "lev": 100, "currency": "EUR", "type": "index"},
    "E50EUR": {"size": 10, "lev": 100, "currency": "EUR", "type": "index"},
    "H33HKD": {"size": 100, "lev": 100, "currency": "HKD", "type": "index"},

    # 3. 虚拟货币
    "BTCUSD": {"size": 1, "lev": 500, "currency": "USD", "type": "crypto"},
    "BCHUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "RPLUSD": {"size": 10000, "lev": 500, "currency": "USD", "type": "crypto"},
    "LTCUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "ETHUSD": {"size": 10, "lev": 500, "currency": "USD", "type": "crypto"},
    "XMRUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "BNBUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "SOLUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "XSIUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "crypto"},
    "DOGUSD": {"size": 100000, "lev": 500, "currency": "USD", "type": "crypto"},
    "ADAUSD": {"size": 10000, "lev": 500, "currency": "USD", "type": "crypto"},
    "AVEUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
    "DSHUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "crypto"},
}

DEFAULT_FOREX_SPEC = {"size": 100000, "lev": 500, "type": "forex"}
DEFAULT_STOCK_SPEC = {"size": 100, "lev": 10, "currency": "USD", "type": "stock"}

# ==================== 工具函数 ====================
def norm_str(x):
    if x is None:
        return ""
    return str(x).strip()

def get_client_ip():
    return request.headers.get('X-Real-Ip') or request.headers.get('X-Forwarded-For', request.remote_addr)

def try_parse_json(raw_body: str):
    cleaned = (raw_body or "").strip()
    if not cleaned:
        return None, None, None, None

    parsed_json = None
    parse_error = None
    parse_error_detail = None
    remaining_data = None

    try:
        decoder = json.JSONDecoder()
        parsed_json, idx = decoder.raw_decode(cleaned)
        remaining = cleaned[idx:].strip()
        if remaining:
            remaining_data = remaining[:200]
            print(f"[WARN] 检测到JSON后剩余数据: {remaining_data}")
    except json.JSONDecodeError as e:
        parse_error = str(e)
        parse_error_detail = traceback.format_exc()
        print(f"[ERR] JSON解析错误: {e}")
        print(f"[ERR] 原始body(前500字符): {cleaned[:500]}")
    except Exception as e:
        parse_error = f"未知异常: {str(e)}"
        parse_error_detail = traceback.format_exc()
        print(f"[ERR] 解析时发生未知异常: {e}")

    return parsed_json, parse_error, parse_error_detail, remaining_data

def detect_category(path: str, parsed_json: dict):
    """按接口路径 + body结构判断分类"""
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
        "account": parsed_json.get("account") if isinstance(parsed_json, dict) else None,
        "server": parsed_json.get("server") if isinstance(parsed_json, dict) else None,
        "balance": parsed_json.get("balance") if isinstance(parsed_json, dict) else None,
        "equity": parsed_json.get("equity") if isinstance(parsed_json, dict) else None,
        "floating_pnl": parsed_json.get("floating_pnl") if isinstance(parsed_json, dict) else None,
        "leverage_used": parsed_json.get("leverage_used") if isinstance(parsed_json, dict) else None,
        "risk_flags": parsed_json.get("risk_flags") if isinstance(parsed_json, dict) else None,
        "exposure_notional": parsed_json.get("exposure_notional") if isinstance(parsed_json, dict) else None,
        "positions": parsed_json.get("positions") if isinstance(parsed_json, dict) else None,
    }

    with history_lock:
        if category == "status":
            history_status.appendleft(record)
        elif category == "positions":
            history_positions.appendleft(record)
        elif category == "report":
            history_report.appendleft(record)
        elif category == "poll":
            history_poll.appendleft(record)
        elif category == "echo":
            history_echo.appendleft(record)

    return parsed_json, record

# ==================== 最新状态接口 ====================
@app.route("/api/latest_status", methods=["GET"])
def api_latest_status():
    symbol_filter = request.args.get("symbol", "").upper()

    detail = None
    latest_quote = None

    with history_lock:
        latest_status_record = history_status[0] if history_status else None
        latest_positions_record = history_positions[0] if history_positions else None

        for record in history_report:
            parsed = record.get("parsed")
            if parsed and parsed.get("desc") == "QUOTE_DATA":
                record_symbol = parsed.get("symbol", "")

                if symbol_filter and record_symbol and record_symbol != symbol_filter:
                    continue

                try:
                    msg = parsed.get("message", "{}")
                    quote_data = json.loads(msg)
                    if isinstance(quote_data, dict) and "bid" in quote_data:
                        latest_quote = {
                            "bid": quote_data.get("bid"),
                            "ask": quote_data.get("ask"),
                            "symbol": record_symbol,
                            "spread": parsed.get("spread"),
                            "ts": parsed.get("ts"),
                            "received_at": record.get("received_at"),
                        }
                        break
                except:
                    pass

        if latest_status_record:
            parsed = latest_status_record.get("parsed", {})
            detail = {
                "received_at": latest_status_record.get("received_at"),
                "account": parsed.get("account"),
                "server": parsed.get("server"),
                "balance": parsed.get("balance"),
                "equity": parsed.get("equity"),
                "margin": parsed.get("margin"),
                "free_margin": parsed.get("free_margin"),
                "margin_level": parsed.get("margin_level"),
                "floating_pnl": parsed.get("floating_pnl"),
                "leverage_used": parsed.get("leverage_used"),
                "positions": parsed.get("positions") or (latest_positions_record.get("parsed", {}).get("positions") if latest_positions_record else []),
            }

    if detail is None and latest_positions_record:
        parsed = latest_positions_record.get("parsed", {})
        detail = {
            "received_at": latest_positions_record.get("received_at"),
            "account": parsed.get("account"),
            "server": parsed.get("server"),
            "balance": None,
            "equity": None,
            "margin": None,
            "free_margin": None,
            "margin_level": None,
            "floating_pnl": None,
            "leverage_used": None,
            "positions": parsed.get("positions") or [],
        }

    return jsonify({
        "detail": detail,
        "latest_quote": latest_quote,
    })

# ==================== MT4 数据接收接口 ====================
@app.route("/web/api/mt4/status", methods=["POST"])
def mt4_status():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    _, _ = store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

@app.route("/web/api/mt4/positions", methods=["POST"])
def mt4_positions():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

@app.route("/web/api/mt4/report", methods=["POST"])
def mt4_report():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

@app.route("/web/api/mt4/quote", methods=["POST"])
def mt4_quote():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

@app.route("/web/api/echo", methods=["POST"])
def mt4_webhook_echo():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return "OK", 200

@app.route("/web/api/mt4/commands", methods=["POST"])
def mt4_commands():
    raw_body = request.get_data(as_text=True)
    client_ip = get_client_ip()
    headers_dict = dict(request.headers)
    store_mt4_data(raw_body, client_ip, headers_dict)
    return jsonify({"commands": []}), 200

# ==================== Tick 推送接口 ====================
@app.route('/api/tick', methods=['POST'])
def receive_tick():
    try:
        ticks = request.json
        if not isinstance(ticks, list):
            ticks = [ticks]

        for tick in ticks:
            symbol = tick.get('symbol')
            bid = tick.get('bid')
            ask = tick.get('ask')
            tick_time = tick.get('tick_time')

            if not symbol or not bid or not ask:
                continue

            quote_msg = json.dumps({
                "bid": bid,
                "ask": ask
            })

            record = {
                "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ip": get_client_ip(),
                "method": "POST",
                "path": "/api/tick",
                "category": "report",
                "headers": {},
                "body_raw": json.dumps(tick),
                "parsed": {
                    "desc": "QUOTE_DATA",
                    "spread": tick.get('spread', 0),
                    "ts": tick_time,
                    "message": quote_msg,
                    "symbol": symbol,
                    "account": "tick_stream"
                }
            }

            with history_lock:
                history_report.appendleft(record)

        return '', 204

    except Exception as e:
        print(f"Error processing tick: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== 产品规格接口 ====================
@app.route("/api/products", methods=["GET"])
def api_products():
    return jsonify(PRODUCT_SPECS)

# ==================== 前端页面 ====================
HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="format-detection" content="telephone=no" />
  <meta name="theme-color" content="#ffffff" />

  <title>量化交易终端 - 移动版</title>
  <style>
    :root {
      --bg: #f5f7fa;
      --card: #ffffff;
      --text: #1a1e23;
      --muted: #848e9c;
      --line: #eaecef;
      --green: #0ecb81;
      --red: #f6465d;
      --yellow: #f0b90b;
      --chip: #f3f5f7;
      --shadow: 0 0.5rem 1.5rem rgba(0,0,0,0.06);
      --radius: 1rem;
      --safe-bottom: env(safe-area-inset-bottom);
    }

    * {
      box-sizing: border-box;
      -webkit-tap-highlight-color: transparent;
      outline: none;
    }

    html {
      font-size: 16px;
      -webkit-text-size-adjust: 100%;
    }

    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", sans-serif;
      color: var(--text);
      background: var(--bg);
      line-height: 1.5;
      overflow-x: hidden;
      width: 100%;
      overscroll-behavior-y: none;
    }

    button, a, input, [role="button"] {
      touch-action: manipulation;
    }

    .app {
      width: 100%;
      max-width: 62.5rem;
      margin: 0 auto;
      min-height: 100vh;
      padding: 1.25rem;
      padding-bottom: calc(1.25rem + var(--safe-bottom));
    }

    /* 顶部 */
    .topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.25rem; }
    .title { font-size: 1.375rem; font-weight: 800; }
    .conn-status {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--muted);
    }
    .conn-dot {
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: #ccc;
      transition: background 0.3s;
    }
    .conn-dot.active { background: var(--green); box-shadow: 0 0 6px var(--green); }
    .conn-dot.warning { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
    .conn-dot.error { background: var(--red); box-shadow: 0 0 6px var(--red); }

    /* 顶部分类 Tabs */
    .tabs-wrapper {
      overflow-x: auto;
      white-space: nowrap;
      margin-bottom: 1.25rem;
      -webkit-overflow-scrolling: touch;
      padding-bottom: 0.3125rem;
      scrollbar-width: none;
    }
    .tabs-wrapper::-webkit-scrollbar { display: none; }

    .tabs { display: inline-flex; gap: 0.5rem; }
    .tab {
      padding: 0.5rem 1rem;
      border-radius: 0.5rem;
      background: transparent;
      color: var(--muted);
      font-weight: 700;
      border: none;
      font-size: 0.9375rem;
      min-height: 2.75rem;
      transition: 0.2s;
      cursor: pointer;
    }
    .tab.active { background: var(--card); color: var(--text); box-shadow: 0 2px 8px rgba(0,0,0,0.05); }

    /* 品种行情行 */
    .symRow {
      display: grid;
      grid-template-columns: 1fr auto 1fr;
      align-items: center;
      margin-bottom: 1.25rem;
      background: var(--card);
      padding: 1.25rem;
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      border: 1px solid var(--line);
    }
    .symLeftTime {
      font-family: monospace;
      font-weight: 700;
      color: var(--muted);
      font-size: 1.75rem;
      justify-self: start;
    }
    .symCenter { display: flex; flex-direction: column; align-items: center; justify-self: center; gap: 0.25rem; }
    .symName { display: flex; align-items: center; gap: 0.625rem; font-size: 1.625rem; font-weight: 800; }
    .symBadge {
      font-size: 0.875rem;
      padding: 0.25rem 0.5rem;
      border-radius: 0.375rem;
      background: var(--text);
      color: #fff;
      font-weight: 700;
      min-height: 2rem;
      display: inline-flex;
      align-items: center;
      cursor: pointer;
    }
    .symRight { display: flex; align-items: center; justify-self: end; }
    .iconBtn.huge-chart {
      width: 5rem;
      height: 5rem;
      border-radius: 1rem;
      border: 2px solid var(--line);
      background: #fff;
      display: grid;
      place-items: center;
      font-size: 2.25rem;
      box-shadow: 0 4px 12px rgba(0,0,0,0.05);
      transition: 0.2s;
      cursor: pointer;
    }
    .iconBtn.huge-chart:active { transform: scale(0.95); }

    /* 价格展示区域 */
    .price-card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 1.5rem;
      margin-bottom: 1.25rem;
    }

    .price-main {
      text-align: center;
      margin-bottom: 1.5rem;
    }
    .price-value {
      font-size: 2.5rem;
      font-weight: 800;
      letter-spacing: -1px;
      color: var(--text);
    }
    .price-suffix {
      font-size: 1rem;
      font-weight: 600;
      color: var(--muted);
      margin-left: 0.25rem;
    }

    .price-detail {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 1rem;
      text-align: center;
    }
    .price-item {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .price-item-label {
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--muted);
    }
    .price-item-value {
      font-size: 1.25rem;
      font-weight: 800;
      font-family: monospace;
    }
    .price-item-value.bid { color: var(--green); }
    .price-item-value.ask { color: var(--red); }
    .price-item-value.spread { color: var(--text); }

    /* 数据延迟检验 */
    .status-bar {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 1rem 1.5rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
    }
    .status-item {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--muted);
    }
    .signal-dot {
      width: 0.625rem;
      height: 0.625rem;
      border-radius: 50%;
      display: inline-block;
      transition: background 0.3s;
    }
    .signal-dot.green { background: var(--green); box-shadow: 0 0 6px var(--green); }
    .signal-dot.yellow { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
    .signal-dot.red { background: var(--red); box-shadow: 0 0 6px var(--red); }

    /* 账户信息 */
    .account-card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 1.5rem;
    }
    .account-title {
      font-size: 0.875rem;
      font-weight: 700;
      color: var(--muted);
      margin-bottom: 1rem;
    }
    .account-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 1rem;
    }
    .account-item {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .account-label {
      font-size: 0.875rem;
      font-weight: 600;
      color: var(--muted);
    }
    .account-value {
      font-weight: 800;
      font-size: 1rem;
      font-family: monospace;
    }

    /* 弹窗 */
    .modalMask {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.5);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 1.25rem;
      z-index: 100;
      backdrop-filter: blur(2px);
    }
    .modal {
      width: min(27.5rem, 100%);
      background: #fff;
      border-radius: 1.25rem;
      overflow: hidden;
      box-shadow: 0 1.25rem 2.5rem rgba(0,0,0,0.1);
      animation: pop 0.2s ease-out;
      display: flex;
      flex-direction: column;
      max-height: 90vh;
    }
    @keyframes pop { from { transform: scale(0.95); opacity: 0; } to { transform: scale(1); opacity: 1; } }

    .modalHeader {
      padding: 1.25rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--line);
      font-weight: 800;
      font-size: 1.125rem;
      flex-shrink: 0;
      min-height: 3.5rem;
    }
    .modalBody { padding: 1.25rem; overflow-y: auto; flex-grow: 1; }
    .select-item {
      padding: 1rem;
      border-bottom: 1px solid var(--line);
      font-weight: 700;
      display: flex;
      justify-content: space-between;
      align-items: center;
      min-height: 3.5rem;
      cursor: pointer;
    }
    .select-item:last-child { border-bottom: none; }
    .select-item:active { background: var(--chip); }
    .select-item.active { color: var(--text); }

    /* 响应式 */
    @media (max-width: 768px) {
      .app { padding: 1rem; padding-bottom: calc(1rem + var(--safe-bottom)); }
      .symRow { padding: 1rem; }
      .symLeftTime { font-size: 1.5rem; }
      .symName { font-size: 1.375rem; }
      .price-value { font-size: 2rem; }
      .iconBtn.huge-chart { width: 4rem; height: 4rem; font-size: 1.75rem; }
      .modalMask { align-items: flex-end; padding: 0; }
      .modal {
        width: 100%;
        max-width: 100%;
        border-radius: 1.5rem 1.5rem 0 0;
        margin: 0;
        animation: slideUp 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        padding-bottom: var(--safe-bottom);
      }
      @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }
    }
  </style>
</head>
<body>

  <div class="app">
    <div class="topbar">
      <div class="title">MT4 量化终端</div>
      <div class="conn-status">
        <span id="connText">等待连接...</span>
        <span class="conn-dot" id="connDot"></span>
      </div>
    </div>

    <div class="tabs-wrapper">
      <div class="tabs">
        <button class="tab" data-category="forex" onclick="switchMainTab(this); showCategoryPairs('forex')">外汇</button>
        <button class="tab" data-category="index" onclick="switchMainTab(this); showCategoryPairs('index')">指数</button>
        <button class="tab" data-category="commodity" onclick="switchMainTab(this); showCategoryPairs('commodity')">大宗商品</button>
        <button class="tab active" data-category="metal" onclick="switchMainTab(this); showCategoryPairs('metal')">贵金属</button>
        <button class="tab" data-category="crypto" onclick="switchMainTab(this); showCategoryPairs('crypto')">虚拟货币</button>
      </div>
    </div>

    <div class="symRow">
      <div class="symLeftTime" id="sysTime">--:--:--</div>
      <div class="symCenter">
        <div class="symName">
          <span id="symName">XAUUSD</span>
          <span class="symBadge" onclick="openCurrentCategoryPairs()">切换品种 ▼</span>
        </div>
      </div>
      <div class="symRight">
        <button class="iconBtn huge-chart" title="查看详情" onclick="alert('功能开发中...')">📊</button>
      </div>
    </div>

    <div class="price-card">
      <div class="price-main">
        <span class="price-value" id="midPrice">--</span>
        <span class="price-suffix" id="priceSuffix"></span>
      </div>
      <div class="price-detail">
        <div class="price-item">
          <span class="price-item-label">买入价 (Bid)</span>
          <span class="price-item-value bid" id="bidPrice">--</span>
        </div>
        <div class="price-item">
          <span class="price-item-label">中间价</span>
          <span class="price-item-value" id="midPriceDetail">--</span>
        </div>
        <div class="price-item">
          <span class="price-item-label">卖出价 (Ask)</span>
          <span class="price-item-value ask" id="askPrice">--</span>
        </div>
      </div>
    </div>

    <div class="status-bar">
      <div class="status-item">
        <span class="signal-dot green" id="latencySignal" title="数据延迟"></span>
        <span id="latencyText">数据延迟: --</span>
      </div>
      <div class="status-item">
        <span id="spreadText">点差: --</span>
      </div>
      <div class="status-item">
        <span id="updateTimeText">更新时间: --</span>
      </div>
    </div>

    <div class="account-card">
      <div class="account-title">账户信息</div>
      <div class="account-grid">
        <div class="account-item">
          <span class="account-label">账户</span>
          <span class="account-value" id="accountVal">--</span>
        </div>
        <div class="account-item">
          <span class="account-label">服务器</span>
          <span class="account-value" id="serverVal">--</span>
        </div>
        <div class="account-item">
          <span class="account-label">余额</span>
          <span class="account-value" id="balanceVal">--</span>
        </div>
        <div class="account-item">
          <span class="account-label">净值</span>
          <span class="account-value" id="equityVal">--</span>
        </div>
      </div>
    </div>
  </div>

  <!-- 品种选择弹窗 -->
  <div class="modalMask" id="pairMask" onclick="closeModal(event, 'pairMask')">
    <div class="modal">
      <div class="modalHeader">
        <span id="pairModalTitle">切换交易品种</span>
        <button class="btn" style="border:none; padding:0.25rem 0.5rem;" onclick="$('pairMask').style.display='none'">✕</button>
      </div>
      <div class="modalBody" id="pairListContainer"></div>
    </div>
  </div>

  <script>
    const $ = id => document.getElementById(id);

    document.addEventListener('touchstart', function(){}, {passive: true});

    // 产品规格
    const PRODUCT_SPECS = {
      "XAGUSD": {"size": 5000, "lev": 500, "currency": "USD", "type": "metal"},
      "XAUUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "metal"},
      "UKOUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "commodity"},
      "USOUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "commodity"},
      "U30USD": {"size": 10, "lev": 100, "currency": "USD", "type": "index"},
      "NASUSD": {"size": 10, "lev": 100, "currency": "USD", "type": "index"},
      "SPXUSD": {"size": 100, "lev": 100, "currency": "USD", "type": "index"},
      "100GBP": {"size": 10, "lev": 100, "currency": "GBP", "type": "index"},
      "D30EUR": {"size": 10, "lev": 100, "currency": "EUR", "type": "index"},
      "E50EUR": {"size": 10, "lev": 100, "currency": "EUR", "type": "index"},
      "H33HKD": {"size": 100, "lev": 100, "currency": "HKD", "type": "index"},
      "BTCUSD": {"size": 1, "lev": 500, "currency": "USD", "type": "crypto"},
      "BCHUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
      "RPLUSD": {"size": 10000, "lev": 500, "currency": "USD", "type": "crypto"},
      "LTCUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
      "ETHUSD": {"size": 10, "lev": 500, "currency": "USD", "type": "crypto"},
      "XMRUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
      "BNBUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
      "SOLUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
      "XSIUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "crypto"},
      "DOGUSD": {"size": 100000, "lev": 500, "currency": "USD", "type": "crypto"},
      "ADAUSD": {"size": 10000, "lev": 500, "currency": "USD", "type": "crypto"},
      "AVEUSD": {"size": 100, "lev": 500, "currency": "USD", "type": "crypto"},
      "DSHUSD": {"size": 1000, "lev": 500, "currency": "USD", "type": "crypto"},
    };

    // 分类产品映射
    const CATEGORY_PAIRS = {
      "forex": [],
      "index": ["U30USD", "NASUSD", "SPXUSD", "100GBP", "D30EUR", "E50EUR", "H33HKD"],
      "commodity": ["UKOUSD", "USOUSD"],
      "metal": ["XAUUSD", "XAGUSD"],
      "crypto": ["BTCUSD", "ETHUSD", "LTCUSD", "BCHUSD", "XMRUSD", "BNBUSD", "SOLUSD", "XSIUSD", "DOGUSD", "ADAUSD", "AVEUSD", "DSHUSD", "RPLUSD"],
    };

    window.currentSymbol = "XAUUSD";
    window.currentCategory = "metal";
    window.lastDataTime = 0;
    window.stagnationCount = 0;

    // 格式化数字
    function fmtNum(n, d) { return parseFloat(n).toFixed(d); }

    // 价格精度
    function getPriceDigits(symbol) {
      if (!symbol) return 2;
      if (symbol === "XAUUSD" || symbol === "XAGUSD" || symbol === "UKOUSD" || symbol === "USOUSD") return 2;
      if (symbol.startsWith("BTC") || symbol === "ETHUSD") return 2;
      return 5;
    }

    // 更新时间显示
    function updateClock() {
      const now = new Date();
      const h = String(now.getHours()).padStart(2, '0');
      const m = String(now.getMinutes()).padStart(2, '0');
      const s = String(now.getSeconds()).padStart(2, '0');
      if ($('sysTime')) $('sysTime').innerText = `${h}:${m}:${s}`;
    }

    // 切换主分类 Tab
    function switchMainTab(btn) {
      document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
    }

    // 显示分类品种列表
    function showCategoryPairs(category) {
      window.currentCategory = category;
    }

    // 打开品种选择弹窗
    function openCurrentCategoryPairs() {
      const container = $('pairListContainer');
      const title = $('pairModalTitle');
      const categoryNames = {
        "forex": "外汇",
        "index": "指数",
        "commodity": "大宗商品",
        "metal": "贵金属",
        "crypto": "虚拟货币"
      };

      title.innerText = `切换${categoryNames[window.currentCategory] || '品种'}`;

      const pairs = CATEGORY_PAIRS[window.currentCategory] || [];
      container.innerHTML = pairs.map(symbol => `
        <div class="select-item ${symbol === window.currentSymbol ? 'active' : ''}" onclick="selectSymbol('${symbol}')">
          <span>${symbol}</span>
          <span style="color: var(--muted); font-size: 0.875rem;">${PRODUCT_SPECS[symbol]?.type || ''}</span>
        </div>
      `).join('');

      $('pairMask').style.display = 'flex';
    }

    // 选择品种
    function selectSymbol(symbol) {
      window.currentSymbol = symbol;
      if ($('symName')) $('symName').innerText = symbol;
      $('pairMask').style.display = 'none';
      // 重置价格显示
      if ($('bidPrice')) $('bidPrice').innerText = '--';
      if ($('askPrice')) $('askPrice').innerText = '--';
      if ($('midPrice')) $('midPrice').innerText = '--';
      if ($('midPriceDetail')) $('midPriceDetail').innerText = '--';
      if ($('spreadText')) $('spreadText').innerText = '点差: --';
      window.lastDataTime = 0;
      window.stagnationCount = 0;
      refreshData();
    }

    // 关闭弹窗
    function closeModal(event, id) {
      if (event.target.id === id) {
        $(id).style.display = 'none';
      }
    }

    // 刷新数据
    async function refreshData() {
      try {
        const response = await fetch(`/api/latest_status?symbol=${window.currentSymbol}`);
        const data = await response.json();

        // 更新连接状态
        updateConnectionStatus(true);

        // 更新账户信息
        if (data.detail) {
          if ($('accountVal')) $('accountVal').innerText = data.detail.account || '--';
          if ($('serverVal')) $('serverVal').innerText = data.detail.server || '--';
          if ($('balanceVal')) $('balanceVal').innerText = data.detail.balance != null ? fmtNum(data.detail.balance, 2) : '--';
          if ($('equityVal')) $('equityVal').innerText = data.detail.equity != null ? fmtNum(data.detail.equity, 2) : '--';
        }

        // 更新价格
        if (data.latest_quote) {
          const quote = data.latest_quote;
          const digits = getPriceDigits(window.currentSymbol);

          if ($('bidPrice')) $('bidPrice').innerText = fmtNum(quote.bid, digits);
          if ($('askPrice')) $('askPrice').innerText = fmtNum(quote.ask, digits);
          if ($('midPrice')) $('midPrice').innerText = fmtNum((quote.bid + quote.ask) / 2, digits);
          if ($('midPriceDetail')) $('midPriceDetail').innerText = fmtNum((quote.bid + quote.ask) / 2, digits);

          // 更新点差
          const spread = quote.spread != null ? quote.spread : ((quote.ask - quote.bid) * Math.pow(10, digits > 2 ? 0 : (5 - digits))).toFixed(1);
          if ($('spreadText')) $('spreadText').innerText = `点差: ${spread}`;

          // 更新时间戳
          if (quote.received_at && $('updateTimeText')) {
            $('updateTimeText').innerText = `更新: ${quote.received_at.split(' ')[1] || '--'}`;
          }

          // 数据延迟检测
          const now = Date.now();
          if (quote.ts) {
            const latency = now - quote.ts;
            window.lastDataTime = quote.ts;

            if ($('latencyText')) $('latencyText').innerText = `延迟: ${latency}ms`;
            updateLatencySignal(latency);
          }
        }

      } catch (error) {
        console.error('获取数据失败:', error);
        updateConnectionStatus(false);
      }
    }

    // 更新延迟信号灯
    function updateLatencySignal(latency) {
      const signal = $('latencySignal');
      if (!signal) return;

      signal.className = 'signal-dot';
      if (latency < 500) {
        signal.classList.add('green');
      } else if (latency < 2000) {
        signal.classList.add('yellow');
      } else {
        signal.classList.add('red');
      }
    }

    // 更新连接状态
    function updateConnectionStatus(connected) {
      const dot = $('connDot');
      const text = $('connText');
      if (!dot || !text) return;

      dot.className = 'conn-dot';
      if (connected) {
        dot.classList.add('active');
        text.innerText = '已连接';
      } else {
        dot.classList.add('error');
        text.innerText = '连接断开';
      }
    }

    // 价格停滞检测
    function checkPriceStagnation() {
      const currentPrice = parseFloat($('midPrice')?.innerText) || 0;
      if (window.lastPriceForSignal === currentPrice && currentPrice > 0) {
        window.stagnationCount++;
        if (window.stagnationCount >= 5) {
          const signal = $('latencySignal');
          if (signal) {
            signal.className = 'signal-dot';
            signal.classList.add('red');
          }
          if ($('latencyText')) $('latencyText').innerText = '数据停滞!';
        }
      } else {
        window.stagnationCount = 0;
      }
      window.lastPriceForSignal = currentPrice;
    }

    // 初始化
    function init() {
      updateClock();
      setInterval(updateClock, 1000);
      refreshData();
      setInterval(refreshData, 2000);
      setInterval(checkPriceStagnation, 2000);
    }

    init();
  </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

# ==================== 启动 ====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
