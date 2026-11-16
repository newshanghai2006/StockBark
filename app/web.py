import json
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .bark import TEMPLATE_FIELDS, send_bark, validate_server, validate_template
from .db import connect, decode, init_db, settings
from .engine import Engine
from .indicators import validate_condition
from .market import normalize_symbol
from .market import now_cn
from .notifications import browser_content, validate_browser_settings


JSON_FIELDS = {"stocks": ("periods",), "rules": ("stock_ids", "periods", "condition", "target_ids"), "targets": ()}


def text_value(data, name, default="", limit=100):
    value = data.get(name, default)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} 不能为空且最多 {limit} 字")
    return value.strip()


def integer(value, low, high, nullable=False):
    if nullable and (value is None or value == ""):
        return None
    if isinstance(value, bool):
        raise ValueError("数值参数不能为布尔值")
    try:
        number = int(value)
        if float(value) != number or not low <= number <= high:
            raise ValueError()
        return number
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"参数必须为 {low}～{high} 的整数") from None


def boolean(value):
    if not isinstance(value, bool):
        raise ValueError("启用选项必须为布尔值")
    return int(value)


def periods(value):
    if not isinstance(value, list) or not value or any(x not in ("day", "5", "15") for x in value):
        raise ValueError("请选择日线、5分钟或15分钟")
    return list(dict.fromkeys(value))


def ids(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("所选目标无效")
    return list(dict.fromkeys(integer(v, 1, 2147483647) for v in value))


def create_app(data_dir=None, password=None, start_engine=False, testing=False):
    app = Flask(__name__)
    directory = Path(data_dir or os.environ.get("DATA_DIR", "data")).resolve()
    db_path = str(directory / "tixing.sqlite3")
    admin_password = password if password is not None else os.environ.get("ADMIN_PASSWORD", "")
    if len(admin_password) < 12 or admin_password == "please-change-this-password":
        raise ValueError("请设置至少 12 位的 ADMIN_PASSWORD 环境变量")
    init_db(db_path)
    with connect(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("INSERT OR IGNORE INTO app_meta VALUES ('session_secret',?)", (secrets.token_hex(32),))
        secret = c.execute("SELECT value FROM app_meta WHERE key='session_secret'").fetchone()[0]
    app.config.update(SECRET_KEY=secret, TESTING=testing, MAX_CONTENT_LENGTH=128 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=7))
    # PBKDF2 avoids scrypt's per-login memory allocation on small ARM devices.
    password_hash = generate_password_hash(admin_password, method="pbkdf2:sha256:600000")
    auth_version = hmac.new(secret.encode(), admin_password.encode(), hashlib.sha256).hexdigest()
    engine = Engine(db_path)
    app.extensions["engine"] = engine
    app.config["DB_PATH"] = db_path
    failures = defaultdict(deque)
    failure_lock = threading.Lock()

    @app.before_request
    def protect():
        if request.path.startswith("/api/"):
            if request.method in ("POST", "PUT", "DELETE"):
                origin = request.headers.get("Origin")
                if origin and urlsplit(origin).netloc != request.host:
                    return jsonify(error="请求来源无效"), 403
                if request.method != "DELETE" and not request.is_json:
                    return jsonify(error="请使用 JSON 请求"), 415
            if request.path != "/api/login":
                if not session.get("authenticated") or not secrets.compare_digest(session.get("auth_version", ""), auth_version):
                    return jsonify(error="请先登录"), 401
                if request.method in ("POST", "PUT", "DELETE") and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "!")):
                    return jsonify(error="会话验证失败，请刷新页面"), 403

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def bad_input(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error="请求内容过大"), 413

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def health():
        with connect(db_path) as c:
            c.execute("SELECT 1").fetchone()
        return jsonify(status="ok")

    @app.post("/api/login")
    def login():
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise ValueError("请求参数无效")
        address = request.remote_addr or "unknown"
        stamp = time.monotonic()
        with failure_lock:
            while failures[address] and failures[address][0] < stamp - 300:
                failures[address].popleft()
            if len(failures[address]) >= 10:
                return jsonify(error="尝试过多，请 5 分钟后再试"), 429
        supplied = payload.get("password", "")
        if not isinstance(supplied, str) or len(supplied) > 1024 or not check_password_hash(password_hash, supplied):
            with failure_lock:
                failures[address].append(stamp)
            return jsonify(error="管理密码不正确"), 401
        with failure_lock:
            failures.pop(address, None)
        session.clear()
        session["authenticated"] = True
        session["auth_version"] = auth_version
        session["csrf"] = secrets.token_hex(24)
        session.permanent = True
        return jsonify(ok=True, csrf=session["csrf"])

    @app.post("/api/logout")
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/state")
    def state():
        with connect(db_path) as c:
            data = {"csrf": session["csrf"], "settings": settings(c), "status": engine.get_status(), "template_fields": sorted(TEMPLATE_FIELDS),
                    "notification_scope": hashlib.sha256(secret.encode()).hexdigest()[:20]}
            for table, fields in JSON_FIELDS.items():
                data[table] = [decode(row, fields) for row in c.execute(f"SELECT * FROM {table} ORDER BY id")]
            for target in data["targets"]:
                target.pop("token")
                target["has_token"] = True
            data["snapshots"] = [decode(r, ("data",)) for r in c.execute("SELECT * FROM snapshots")]
        return jsonify(data)

    @app.put("/api/settings")
    def save_settings():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("配置格式无效")
        config = {
            "poll_seconds": integer(data.get("poll_seconds"), 30, 3600),
            "market_only": bool(boolean(data.get("market_only"))),
            "completed_only": bool(boolean(data.get("completed_only"))),
            "kdj_n": integer(data.get("kdj_n"), 2, 120),
            "kdj_m1": integer(data.get("kdj_m1"), 1, 60),
            "kdj_m2": integer(data.get("kdj_m2"), 1, 60),
            "cooldown_minutes": integer(data.get("cooldown_minutes"), 0, 10080),
            "provider": data.get("provider"),
        }
        if config["provider"] not in ("auto", "eastmoney", "tencent"):
            raise ValueError("行情源无效")
        with connect(db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            config.update(validate_browser_settings(data, settings(c)))
            c.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(config),))
        return jsonify(ok=True)

    @app.get("/api/browser-events")
    def browser_events():
        raw_after = request.args.get("after")
        after = integer(raw_after, 0, 9223372036854775807) if raw_after is not None else None
        cutoff = (now_cn() - timedelta(minutes=5)).isoformat()
        with connect(db_path) as c:
            c.execute("BEGIN")
            enabled = settings(c)["browser_enabled"]
            head = c.execute("SELECT COALESCE(MAX(id),0) FROM alerts").fetchone()[0]
            # No cursor means establish a baseline, never replay historical alerts.
            if after is None or not enabled:
                return jsonify(enabled=enabled, cursor=head, items=[])
            rows = [dict(r) for r in c.execute("""SELECT a.id,a.browser_title AS title,a.browser_body AS body,a.created_at
                FROM alerts a JOIN rules r ON r.id=a.rule_id AND r.enabled=1
                JOIN stocks s ON s.symbol=a.symbol AND s.enabled=1
                WHERE a.id>? AND a.id<=? AND a.browser_title IS NOT NULL AND a.created_at>=?
                ORDER BY a.id LIMIT 100""", (after, head, cutoff))]
        cursor = rows[-1]["id"] if len(rows) == 100 else head
        return jsonify(enabled=enabled, cursor=cursor, items=rows)

    @app.post("/api/browser-preview")
    def browser_preview():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("配置格式无效")
        with connect(db_path) as c:
            config = validate_browser_settings(data, settings(c))
        values = {"symbol": "sh688605", "code": "688605", "name": "先锋精科", "period": "5分钟",
                  "rule": "J 高位提醒", "K": 85.0, "D": 75.0, "J": 105.0, "RSV": 90.0,
                  "close": 50.0, "bar_time": "示例 K 线时间", "time": now_cn().strftime("%Y-%m-%d %H:%M:%S")}
        from .bark import render_template
        from .db import DEFAULT_BODY
        title, body = browser_content(config, render_template("{name} · {rule}", values), render_template(DEFAULT_BODY, values), values)
        return jsonify(title="[测试] " + title, body=body + "\n仅为模板测试，不代表真实行情。")

    @app.post("/api/<table>")
    @app.put("/api/<table>/<int:row_id>")
    def save_row(table, row_id=None):
        if table not in JSON_FIELDS:
            return jsonify(error="没有这个资源"), 404
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("配置格式无效")
        with connect(db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone() if row_id else None
            if row_id and existing is None:
                return jsonify(error="记录不存在"), 404
            if not row_id and c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= {"stocks": 60, "rules": 100, "targets": 20}[table]:
                raise ValueError("记录数量已达上限")
            enabled = boolean(data.get("enabled", True))
            name = text_value(data, "name", limit=80)
            if table == "stocks":
                symbol = normalize_symbol(data.get("symbol", ""))
                duplicate = c.execute("SELECT id FROM stocks WHERE symbol=? AND id!=?", (symbol, row_id or 0)).fetchone()
                if duplicate:
                    raise ValueError("该股票已存在")
                values = {"name": name, "symbol": symbol, "enabled": enabled, "periods": json.dumps(periods(data.get("periods"))),
                          **{k: integer(data.get(k), 2 if k == "kdj_n" else 1, 120 if k == "kdj_n" else 60, True) for k in ("kdj_n", "kdj_m1", "kdj_m2")}}
            elif table == "targets":
                token = data.get("token", "")
                if not isinstance(token, str):
                    raise ValueError("设备 Key 无效")
                token = token.strip() or (existing["token"] if existing else "")
                if not token or len(token) > 512 or any(ch.isspace() for ch in token) or "/" in token:
                    raise ValueError("请填写 Bark 设备 Key，勿粘贴完整推送链接")
                sound, group = data.get("sound", ""), data.get("group_name", "股票提醒")
                if not isinstance(sound, str) or not isinstance(group, str) or len(sound) > 80 or len(group) > 80:
                    raise ValueError("铃声或分组名称无效")
                level = data.get("level", "active")
                if level not in ("active", "passive", "timeSensitive"):
                    raise ValueError("推送等级无效")
                values = {"name": name, "server": validate_server(data.get("server", "https://api.day.app")), "token": token,
                          "enabled": enabled, "sound": sound, "group_name": group, "level": level}
            else:
                mode = data.get("mode", "on_enter")
                if mode not in ("on_enter", "per_bar"):
                    raise ValueError("触发方式无效")
                stock_ids, target_ids = ids(data.get("stock_ids", [])), ids(data.get("target_ids", []))
                for ref_table, selected in (("stocks", stock_ids), ("targets", target_ids)):
                    valid = {r[0] for r in c.execute(f"SELECT id FROM {ref_table}")}
                    # Preserve a previously deleted reference; it must never become "all".
                    old = json.loads(existing["stock_ids" if ref_table == "stocks" else "target_ids"]) if existing else []
                    if any(v not in valid and v not in old for v in selected):
                        raise ValueError("所选股票或设备不存在")
                values = {"name": name, "enabled": enabled, "mode": mode,
                          "stock_ids": json.dumps(stock_ids), "target_ids": json.dumps(target_ids),
                          "periods": json.dumps(periods(data.get("periods"))),
                          "condition": json.dumps(validate_condition(data.get("condition"))),
                          "cooldown_minutes": integer(data.get("cooldown_minutes"), 0, 10080, True),
                          "title": validate_template(data.get("title"), 200), "body": validate_template(data.get("body"))}
            if row_id:
                assignments = ",".join(key + "=?" for key in values)
                c.execute(f"UPDATE {table} SET {assignments} WHERE id=?", (*values.values(), row_id))
                if table == "rules":
                    c.execute("DELETE FROM rule_state WHERE rule_id=?", (row_id,))
                elif table == "stocks":
                    c.execute("DELETE FROM rule_state WHERE stock_id=?", (row_id,))
                    c.execute("DELETE FROM snapshots WHERE stock_id=?", (row_id,))
            else:
                columns = ",".join(values)
                marks = ",".join("?" for _ in values)
                row_id = c.execute(f"INSERT INTO {table}({columns}) VALUES ({marks})", tuple(values.values())).lastrowid
        return jsonify(ok=True, id=row_id)

    @app.delete("/api/<table>/<int:row_id>")
    def delete_row(table, row_id):
        if table not in JSON_FIELDS:
            return jsonify(error="没有这个资源"), 404
        with connect(db_path) as c:
            c.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
        return jsonify(ok=True)

    @app.post("/api/targets/<int:row_id>/test")
    def test_target(row_id):
        with connect(db_path) as c:
            target = c.execute("SELECT * FROM targets WHERE id=?", (row_id,)).fetchone()
        if target is None:
            return jsonify(error="设备不存在"), 404
        send_bark(dict(target), "股票提醒 · 测试消息", "Bark 连接成功。正式提醒将按你配置的股票、周期和条件发送。")
        return jsonify(ok=True)

    @app.post("/api/check")
    def check():
        engine.request_run()
        return jsonify(ok=True, message="已安排检查，满足条件时将推送")

    @app.get("/api/history")
    def history():
        page = integer(request.args.get("page", 1), 1, 100000)
        with connect(db_path) as c:
            total = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            rows = [decode(r, ("values_json",)) for r in c.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 25 OFFSET ?", ((page - 1) * 25,))]
            for row in rows:
                row["deliveries"] = [dict(r) for r in c.execute("SELECT * FROM deliveries WHERE alert_id=?", (row["id"],))]
        return jsonify(items=rows, total=total, page=page)

    if start_engine:
        engine.start()
    return app
