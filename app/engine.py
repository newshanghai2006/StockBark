import json
import logging
import threading
import time

from .bark import render_template, send_bark
from .db import connect, decode, settings
from .indicators import kdj, matches
from .market import MarketClient, PERIOD_NAMES, closed_bars, in_market, is_stale, now_cn
from .notifications import browser_content


log = logging.getLogger(__name__)


class Engine:
    def __init__(self, db_path, market=None, sender=None, clock=None):
        self.db_path = db_path
        self.market = market or MarketClient()
        self.sender = sender or send_bark
        self.clock = clock or now_cn
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = None
        self.status_lock = threading.Lock()
        self.status = {"running": False, "last_run": None, "next_run": None, "message": "等待首次检查"}

    def get_status(self):
        with self.status_lock:
            return dict(self.status)

    def set_status(self, **values):
        with self.status_lock:
            self.status.update(values)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="stock-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def request_run(self):
        self.wake_event.set()

    def _loop(self):
        manual = False
        while not self.stop_event.is_set():
            try:
                self.run_once(manual=manual)
            except Exception:
                log.exception("Monitoring iteration failed")
                self.set_status(message="检查失败，请查看服务日志")
            with connect(self.db_path) as c:
                delay = settings(c)["poll_seconds"]
            self.set_status(next_run=self.clock().timestamp() + delay)
            manual = self.wake_event.wait(delay)
            self.wake_event.clear()

    def run_once(self, manual=False):
        if not self.lock.acquire(blocking=False):
            return False
        self.set_status(running=True)
        try:
            now = self.clock()
            self.retry_deliveries(now.timestamp())
            with connect(self.db_path) as c:
                config = settings(c)
                stocks = [decode(r, ("periods",)) for r in c.execute("SELECT * FROM stocks WHERE enabled=1")]
                rules = [decode(r, ("periods", "stock_ids", "condition", "target_ids")) for r in c.execute("SELECT * FROM rules WHERE enabled=1")]
            if config["market_only"] and not manual and not in_market(now):
                self.set_status(message="非交易时段，等待开市（可手动检查）")
                return True
            successful = errors = 0
            for stock in stocks:
                for period in stock["periods"]:
                    if self.stop_event.is_set():
                        return True
                    try:
                        bars, actual_name, source = self.market.fetch(stock["symbol"], period, config["provider"])
                        if config["completed_only"]:
                            bars = closed_bars(bars, period, now)
                        n, m1, m2 = (stock.get(key) or config[key] for key in ("kdj_n", "kdj_m1", "kdj_m2"))
                        if len(bars) < n + 30:
                            raise ValueError(f"有效 K 线不足，至少需要 {n + 30} 根用于指标预热")
                        series = kdj(bars, n, m1, m2)
                        current, previous = series[-1], series[-2]
                        # Outside the latest trading date, only display the snapshot.
                        stale = is_stale(current, period, now)
                        signals = [r["name"] for r in rules if period in r["periods"] and (not r["stock_ids"] or stock["id"] in r["stock_ids"]) and matches(r["condition"], current, previous)]
                        history = [{k: point[k] for k in ("time", "K", "D", "J")} for point in series[-48:]]
                        values = {**current, "source": source, "stale": stale, "name": actual_name, "signals": signals, "history": history,
                                  "kdj_n": n, "kdj_m1": m1, "kdj_m2": m2, "bars": len(bars)}
                        with connect(self.db_path) as c:
                            # It may have been removed while the network request was running.
                            if not c.execute("SELECT 1 FROM stocks WHERE id=?", (stock["id"],)).fetchone():
                                continue
                            c.execute("""INSERT INTO snapshots VALUES (?,?,?,?,?) ON CONFLICT(stock_id,period)
                                DO UPDATE SET data=excluded.data,error=NULL,checked_at=excluded.checked_at""",
                                      (stock["id"], period, json.dumps(values), None, now.isoformat()))
                        if not stale:
                            for rule in rules:
                                if period in rule["periods"] and (not rule["stock_ids"] or stock["id"] in rule["stock_ids"]):
                                    self.evaluate(rule, stock, period, current, previous, config, now)
                        successful += 1
                    except (ValueError, KeyError, TypeError) as exc:
                        errors += 1
                        with connect(self.db_path) as c:
                            if c.execute("SELECT 1 FROM stocks WHERE id=?", (stock["id"],)).fetchone():
                                c.execute("""INSERT INTO snapshots VALUES (?,?,?,?,?) ON CONFLICT(stock_id,period)
                                    DO UPDATE SET error=excluded.error,checked_at=excluded.checked_at""",
                                          (stock["id"], period, None, str(exc), now.isoformat()))
            self.retry_deliveries(now.timestamp())
            self.cleanup(now.timestamp())
            self.set_status(last_run=now.isoformat(), message=f"已检查 {successful} 个周期，{errors} 个失败")
            return True
        finally:
            self.set_status(running=False)
            self.lock.release()

    def evaluate(self, rule, stock, period, current, previous, config, now):
        hit = matches(rule["condition"], current, previous)
        epoch = now.timestamp()
        with connect(self.db_path) as c:
            c.execute("BEGIN IMMEDIATE")
            # Configuration edits invalidate this iteration's copied rule/stock.
            fresh_rule = c.execute("SELECT * FROM rules WHERE id=?", (rule["id"],)).fetchone()
            fresh_stock = c.execute("SELECT * FROM stocks WHERE id=?", (stock["id"],)).fetchone()
            if not fresh_rule or not fresh_stock or decode(fresh_rule, ("periods", "stock_ids", "condition", "target_ids")) != rule or decode(fresh_stock, ("periods",)) != stock:
                return
            state = c.execute("SELECT * FROM rule_state WHERE rule_id=? AND stock_id=? AND period=?", (rule["id"], stock["id"], period)).fetchone()
            # A first matching observation is actionable, even after a restart.
            was_hit = bool(state["matched"]) if state else False
            last_sent = state["last_sent"] if state else 0
            cooldown = rule["cooldown_minutes"] if rule["cooldown_minutes"] is not None else config["cooldown_minutes"]
            should_send = hit and (rule["mode"] == "per_bar" or not was_hit) and epoch - last_sent >= cooldown * 60
            targets = [dict(r) for r in c.execute("SELECT * FROM targets WHERE enabled=1")]
            if rule["target_ids"]:
                targets = [t for t in targets if t["id"] in rule["target_ids"]]
            # Read inside this transaction so disabling browser alerts takes effect
            # even if the current scan started with older settings.
            browser_config = settings(c)
            browser_enabled = browser_config["browser_enabled"]
            has_channel = bool(targets) or browser_enabled
            if should_send and has_channel:
                values = {**current, "symbol": stock["symbol"], "code": stock["symbol"][2:], "name": stock["name"],
                          "period": PERIOD_NAMES[period], "rule": rule["name"], "bar_time": current["time"], "time": now.strftime("%Y-%m-%d %H:%M:%S")}
                title, body = render_template(rule["title"], values), render_template(rule["body"], values)
                desktop_title, desktop_body = browser_content(browser_config, title, body, values) if browser_enabled else (None, None)
                cursor = c.execute("""INSERT OR IGNORE INTO alerts(rule_id,rule_name,symbol,stock_name,period,bar_time,title,body,values_json,created_at,browser_title,browser_body)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (rule["id"], rule["name"], stock["symbol"], stock["name"], period, current["time"], title, body, json.dumps(current), now.isoformat(), desktop_title, desktop_body))
                if cursor.rowcount:
                    for target in targets:
                        c.execute("INSERT INTO deliveries(alert_id,target_id,target_name) VALUES (?,?,?)", (cursor.lastrowid, target["id"], target["name"]))
                    last_sent = epoch
            # With no enabled channel, preserve the edge for later configuration.
            remembered = hit if has_channel else was_hit
            c.execute("""INSERT INTO rule_state VALUES (?,?,?,?,?) ON CONFLICT(rule_id,stock_id,period)
                DO UPDATE SET matched=excluded.matched,last_sent=excluded.last_sent""", (rule["id"], stock["id"], period, int(remembered), last_sent))

    def retry_deliveries(self, epoch):
        with connect(self.db_path) as c:
            deliveries = [dict(r) for r in c.execute("""SELECT d.*,a.title,a.body,a.created_at,a.rule_id,a.symbol FROM deliveries d
                JOIN alerts a ON a.id=d.alert_id WHERE d.status='pending' AND d.next_retry<=? ORDER BY d.id LIMIT 40""", (epoch,))]
        for delivery in deliveries:
            with connect(self.db_path) as c:
                target = c.execute("SELECT * FROM targets WHERE id=? AND enabled=1", (delivery["target_id"],)).fetchone()
                active_rule = c.execute("SELECT 1 FROM rules WHERE id=? AND enabled=1", (delivery["rule_id"],)).fetchone()
                active_stock = c.execute("SELECT 1 FROM stocks WHERE symbol=? AND enabled=1", (delivery["symbol"],)).fetchone()
            error = None
            # Old queued signals should not be delivered as current market advice.
            expired = epoch - parse_iso_timestamp(delivery["created_at"]) > 3600
            cancelled = not active_rule or not active_stock
            if cancelled:
                error = "规则或股票已停用或删除"
            elif expired:
                error = "信号超过一小时，已停止重试"
            elif target is None:
                error = "接收设备已停用或删除"
            else:
                try:
                    self.sender(dict(target), delivery["title"], delivery["body"])
                except ValueError as exc:
                    error = str(exc)
            attempts = delivery["attempts"] + 1
            status = "sent" if error is None else "failed" if attempts >= 3 or target is None or expired or cancelled else "pending"
            with connect(self.db_path) as c:
                c.execute("UPDATE deliveries SET status=?,attempts=?,error=?,next_retry=?,sent_at=? WHERE id=?", (
                    status, attempts, error, epoch + (60 if attempts == 1 else 300), self.clock().isoformat() if status == "sent" else None, delivery["id"]))

    def cleanup(self, epoch):
        from datetime import datetime
        cutoff = datetime.fromtimestamp(epoch - 90 * 86400, self.clock().tzinfo).isoformat()
        with connect(self.db_path) as c:
            c.execute("DELETE FROM alerts WHERE created_at<?", (cutoff,))


def parse_iso_timestamp(value):
    from datetime import datetime
    return datetime.fromisoformat(value).timestamp()
