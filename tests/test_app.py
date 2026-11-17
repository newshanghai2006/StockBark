import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import requests

from app.bark import render_template, send_bark, validate_template
from app.db import connect, decode, init_db, settings
from app.engine import Engine
from app.indicators import kdj, matches, validate_condition
from app.market import MarketClient, SHANGHAI, closed_bars, in_market, is_stale, normalize_symbol
from app.market import now_cn
from app.web import create_app


def bar(time="2026-11-12 10:00", close=10):
    return {"time": time, "open": close, "close": close, "high": close + 1, "low": close - 1}


class IndicatorsTest(unittest.TestCase):
    def test_kdj_known_sequence(self):
        bars = [{**bar(), "low": 0, "high": 10, "close": 10} for _ in range(10)]
        values = kdj(bars)
        self.assertEqual(len(values), 2)
        self.assertAlmostEqual(values[0]["K"], 66.6666666667)
        self.assertAlmostEqual(values[0]["D"], 55.5555555556)
        self.assertAlmostEqual(values[0]["J"], 88.8888888889)
        self.assertAlmostEqual(values[1]["J"], 107.4074074074)

    def test_flat_market_and_warmup(self):
        bars = [{**bar(), "high": 10, "low": 10} for _ in range(20)]
        self.assertEqual(kdj(bars)[-1]["J"], 50)
        self.assertEqual(kdj(bars[:5]), [])

    def test_both_user_conditions_strict_boundaries(self):
        high = validate_condition({"logic":"all", "items":[{"left":"J","op":">","right":98},{"left":"K","op":">","right":"D"}]})
        low = validate_condition({"logic":"all", "items":[{"left":"J","op":"<","right":2},{"left":"K","op":"<","right":"D"}]})
        self.assertTrue(matches(high, {"J":99,"K":80,"D":70}))
        self.assertFalse(matches(high, {"J":98,"K":80,"D":70}))
        self.assertFalse(matches(high, {"J":99,"K":70,"D":70}))
        self.assertTrue(matches(low, {"J":1,"K":20,"D":30}))
        self.assertFalse(matches(low, {"J":2,"K":20,"D":30}))

    def test_crosses_and_or(self):
        cond = {"logic":"any","items":[{"left":"K","op":"cross_above","right":"D"},{"left":"J","op":"<","right":0}]}
        self.assertTrue(matches(cond, {"K":51,"D":50,"J":20}, {"K":49,"D":50,"J":20}))
        self.assertFalse(matches(cond, {"K":51,"D":50,"J":20}))
        self.assertTrue(matches(cond, {"K":40,"D":50,"J":-1}))
        down = {"logic":"all","items":[{"left":"J","op":"cross_below","right":2}]}
        self.assertTrue(matches(down, {"J":1}, {"J":2}))

    def test_rule_validation_no_eval_or_nonfinite(self):
        for right in ("__import__('os')", float("nan"), True, None):
            with self.assertRaises(ValueError):
                validate_condition({"logic":"all","items":[{"left":"J","op":">","right":right}]})
        for template in ("{J.__class__}", "{J:1000000f}", "{missing}", "{J!r}", "{"):
            with self.assertRaises(ValueError): validate_template(template)
        self.assertEqual(render_template("{name} J={J}", {"name":"股票", "J":100.124}), "股票 J=100.12")


class MarketTest(unittest.TestCase):
    def test_symbol_and_sessions(self):
        self.assertEqual(normalize_symbol("688605"), "sh688605")
        self.assertEqual(normalize_symbol("000001"), "sz000001")
        self.assertEqual(normalize_symbol("920001"), "bj920001")
        self.assertEqual(normalize_symbol("bj430047"), "bj430047")
        with self.assertRaises(ValueError): normalize_symbol("AAPL")
        self.assertTrue(in_market(datetime(2026,11,12,10,0,tzinfo=SHANGHAI)))
        self.assertFalse(in_market(datetime(2026,11,12,12,0,tzinfo=SHANGHAI)))
        self.assertFalse(in_market(datetime(2026,11,14,10,0,tzinfo=SHANGHAI)))

    def test_completed_minute_and_daily(self):
        now = datetime(2026,11,12,10,1,tzinfo=SHANGHAI)
        self.assertEqual(len(closed_bars([bar("2026-11-12 10:00"),bar("2026-11-12 10:05")], "5", now)), 1)
        self.assertEqual(closed_bars([bar("2026-11-12")], "day", now), [])
        self.assertEqual(len(closed_bars([bar("2026-11-12")], "day", now.replace(hour=15,minute=1))), 1)

    def test_stale_minutes_ignore_lunch_but_reject_old_intraday(self):
        now = datetime(2026,11,12,13,1,tzinfo=SHANGHAI)
        self.assertFalse(is_stale(bar("2026-11-12 11:30"), "5", now))
        self.assertTrue(is_stale(bar("2026-11-12 10:00"), "5", now))
        self.assertTrue(is_stale(bar("2026-11-11 15:00"), "5", now))

    def test_eastmoney_and_tencent_parsing_and_fallback(self):
        market = MarketClient()
        east = Mock(); east.json.return_value = {"data":{"name":"先锋精科", "klines":["2026-11-12 10:00,10,11,12,9,100"]}}
        market.session.get = Mock(return_value=east)
        bars, name, source = market.fetch("sh688605", "5")
        self.assertEqual((bars[0]["close"],name,source), (11,"先锋精科","eastmoney"))
        tencent = Mock(); tencent.json.return_value = {"data":{"sh688605":{"m5":[["202611121000","10","11","12","9","100"]],"qt":{"sh688605":["1","先锋精科"]}}}}
        market.session.get = Mock(side_effect=[requests.ConnectionError("private-url"),tencent])
        bars, _, source = market.fetch("sh688605", "5")
        self.assertEqual(source, "tencent")
        self.assertEqual(bars[0]["time"], "2026-11-12 10:00")

    def test_errors_are_safe(self):
        market = MarketClient()
        market.session.get = Mock(side_effect=requests.ConnectionError("https://secret:password@proxy"))
        with self.assertLogs("app.market", level="WARNING") as logs:
            with self.assertRaises(ValueError) as ctx: market.fetch("sh688605", "5")
        self.assertNotIn("password", str(ctx.exception))
        self.assertNotIn("password", " ".join(logs.output))
        self.assertIn("eastmoney、tencent、sina", str(ctx.exception))

    def test_sina_fallback_and_second_precision_timestamps(self):
        for period, scale in (("day", 240), ("5", 5), ("15", 15)):
            with self.subTest(period=period):
                market = MarketClient()
                response = Mock()
                response.text = '/* upstream comment */ IO.X=([{"day":"2026-11-12 10:00:00","open":"10","close":"11","high":"12","low":"9"}]);'
                market.session.get = Mock(side_effect=[requests.ConnectionError(), requests.Timeout(), response])
                with self.assertLogs("app.market", level="WARNING"):
                    bars, name, source = market.fetch("sh605117", period)
                self.assertEqual(source, "sina")
                self.assertEqual(name, "")
                self.assertEqual(bars, [{"time": "2026-11-12" if period == "day" else "2026-11-12 10:00",
                                         "open": 10, "close": 11, "high": 12, "low": 9}])
                call = market.session.get.call_args.kwargs
                self.assertEqual(call["params"]["scale"], scale)
                self.assertEqual(call["params"]["datalen"], 320)
                self.assertEqual(call["headers"]["Referer"], "https://finance.sina.com.cn/")

    def test_empty_or_malformed_sources_fall_back_to_sina(self):
        market = MarketClient()
        east, tencent, sina = Mock(), Mock(), Mock()
        east.json.return_value = {"data": {"klines": []}}
        tencent.json.return_value = {"data": {"sh605117": {"qfqday": [["2026-11-12", "1", "2", "3", "1"]]}}}
        sina.text = 'IO.X=([{"day":"2026-11-12","open":"10","close":"11","high":"12","low":"9"}]);'
        market.session.get = Mock(side_effect=[east, tencent, sina])
        with self.assertLogs("app.market", level="WARNING"):
            bars, _, source = market.fetch("sh605117", "day")
        self.assertEqual((source, bars[0]["close"]), ("sina", 11))

    def test_sina_invalid_empty_or_nonfinite_data_is_rejected(self):
        for text in ('<html>blocked</html>', 'IO.X=([])', 'IO.X=([invalid])',
                     'IO.X=([{"day":"2026-11-12","open":"10","close":"NaN","high":"12","low":"9"}])'):
            with self.subTest(text=text):
                market = MarketClient()
                response = Mock(text=text)
                market.session.get = Mock(return_value=response)
                with self.assertLogs("app.market", level="WARNING"):
                    with self.assertRaisesRegex(ValueError, "行情获取失败（sina）"):
                        market.fetch("sh605117", "day", "sina")
                self.assertEqual(market.session.get.call_count, 1)

    def test_tencent_daily_keeps_unadjusted_prices_and_correct_referer(self):
        market = MarketClient()
        response = Mock()
        response.json.return_value = {"data": {"sh605117": {
            "day": [["2026-11-12", "10", "11", "12", "9"]],
            "qfqday": [["2026-11-12", "1", "2", "3", "1"]]}}}
        market.session.get = Mock(return_value=response)
        bars, _, source = market.fetch("sh605117", "day", "tencent")
        self.assertEqual((source, bars[0]["close"]), ("tencent", 11))
        call = market.session.get.call_args.kwargs
        self.assertEqual(call["params"]["param"], "sh605117,day,,,320,")
        self.assertEqual(call["headers"]["Referer"], "https://gu.qq.com/")

    def test_invalid_provider_is_rejected_before_network_request(self):
        market = MarketClient()
        market.session.get = Mock()
        with self.assertRaisesRegex(ValueError, "行情源无效"):
            market.fetch("sh605117", "day", "invalid")
        market.session.get.assert_not_called()


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name, "test-password-1234", testing=True)
        self.client = self.app.test_client()
        result = self.client.post("/api/login", json={"password":"test-password-1234"})
        self.headers = {"X-CSRF-Token":result.json["csrf"]}
        self.db = self.app.config["DB_PATH"]

    def tearDown(self): self.tmp.cleanup()

    def post(self, path, data): return self.client.post(path, json=data, headers=self.headers)
    def put(self, path, data): return self.client.put(path, json=data, headers=self.headers)

    def add_target(self, name="iPhone"):
        response = self.post("/api/targets", {"name":name,"server":"https://api.day.app","token":"secret-key","enabled":True})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["id"]

    def test_login_csrf_and_origin(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/state").status_code, 401)
        self.assertEqual(anonymous.post("/api/login", json={"password":"wrong"}).status_code,401)
        self.assertEqual(self.client.post("/api/check",json={}).status_code,403)
        self.assertEqual(self.client.post("/api/check",json={},headers={**self.headers,"Origin":"https://evil.test"}).status_code,403)
        self.assertEqual(self.post("/api/check",{}).status_code,200)
        self.assertEqual(self.client.get("/").status_code,200)
        self.assertEqual(self.client.get("/healthz").json["status"],"ok")

    def test_seed_and_persistence(self):
        state = self.client.get("/api/state").json
        self.assertEqual(state["stocks"][0]["symbol"],"sh688605")
        self.assertEqual(len(state["rules"]),2)
        self.assertEqual(state["settings"]["kdj_n"],9)
        self.assertEqual(self.client.delete("/api/stocks/1",headers=self.headers).status_code,200)
        init_db(self.db)
        self.assertEqual(self.client.get("/api/state").json["stocks"],[])

    def test_stock_crud_and_duplicate(self):
        data = {"name":"平安银行","symbol":"000001","periods":["5","15"],"enabled":True,"kdj_n":12}
        response = self.post("/api/stocks",data)
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.post("/api/stocks",data).status_code,400)
        stock_id=response.json["id"]
        self.assertEqual(self.put(f"/api/stocks/{stock_id}",{**data,"kdj_n":15}).status_code,200)
        state=self.client.get("/api/state").json
        self.assertEqual(state["stocks"][-1]["kdj_n"],15)
        self.assertEqual(self.post("/api/stocks",{**data,"symbol":"bad"}).status_code,400)

    def test_target_secrets_never_returned_and_blank_preserves(self):
        target_id=self.add_target()
        response=self.client.get("/api/state")
        self.assertNotIn("secret-key",response.get_data(as_text=True))
        target=response.json["targets"][0]
        self.assertNotIn("token",target)
        self.assertEqual(self.put(f"/api/targets/{target_id}",{**target,"token":""}).status_code,200)
        with connect(self.db) as c:
            self.assertEqual(c.execute("SELECT token FROM targets").fetchone()[0],"secret-key")

    def test_changed_password_invalidates_existing_sessions(self):
        changed = create_app(self.tmp.name, "changed-password-1234", testing=True)
        cookie = self.client.get_cookie("session")
        other_client = changed.test_client()
        other_client.set_cookie("session", cookie.value)
        self.assertEqual(other_client.get("/api/state").status_code, 401)

    def test_invalid_templates_parameters_and_rule_roundtrip(self):
        state=self.client.get("/api/state").json
        rule=state["rules"][0]
        self.assertEqual(self.put("/api/rules/1",{**rule,"title":"{name.__class__}"}).status_code,400)
        self.assertEqual(self.put("/api/settings",{**state["settings"],"poll_seconds":1}).status_code,400)
        self.assertEqual(self.put("/api/settings",{**state["settings"],"poll_seconds":30}).status_code,200)
        self.assertEqual(self.put("/api/rules/1",{**rule,"condition":{"logic":"any","items":[{"left":"K","op":"cross_below","right":"D"}]}}).status_code,200)

    def test_failed_snapshot_can_be_loaded(self):
        with connect(self.db) as c: c.execute("INSERT INTO snapshots VALUES(1,'5',NULL,'网络错误','2026-11-12T10:00:00+08:00')")
        response=self.client.get("/api/state")
        self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json["snapshots"][0]["data"])

    def test_sina_provider_settings_roundtrip(self):
        config = self.client.get("/api/state").json["settings"]
        self.assertEqual(self.put("/api/settings", {**config, "provider": "sina"}).status_code, 200)
        self.assertEqual(self.client.get("/api/state").json["settings"]["provider"], "sina")
        self.assertEqual(self.put("/api/settings", {**config, "provider": "invalid"}).status_code, 400)

    def test_id_not_reused_after_deletion(self):
        first=self.add_target()
        self.client.delete(f"/api/targets/{first}",headers=self.headers)
        self.assertGreater(self.add_target("new"),first)

    def test_browser_settings_default_and_validation(self):
        original = self.client.get("/api/state").json["settings"]
        self.assertFalse(original["browser_enabled"])
        configured = {**original, "browser_enabled": True, "browser_template_mode": "custom",
                      "browser_title": "{name} / {rule}", "browser_body": "J={J} 价格={close}"}
        self.assertEqual(self.put("/api/settings", configured).status_code, 200)
        saved = self.client.get("/api/state").json["settings"]
        self.assertEqual(saved, configured)
        for invalid in ({"browser_title": "{J.__class__}"}, {"browser_template_mode": "eval"}, {"browser_enabled": "false"}):
            self.assertEqual(self.put("/api/settings", {**saved, **invalid}).status_code, 400)
        # Older clients may save their known settings without erasing new options.
        old_client = {k: v for k, v in original.items() if not k.startswith("browser_")}
        self.assertEqual(self.put("/api/settings", old_client).status_code, 200)
        self.assertTrue(self.client.get("/api/state").json["settings"]["browser_enabled"])

    def test_browser_preview_uses_validated_unsaved_template_without_alert(self):
        preview = self.post("/api/browser-preview", {"browser_template_mode": "custom",
                            "browser_title": "{name} {period}", "browser_body": "J={J} K={K} D={D}"})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json["title"], "[测试] 先锋精科 5分钟")
        self.assertIn("J=105.00", preview.json["body"])
        self.assertIn("不代表真实行情", preview.json["body"])
        self.assertEqual(self.client.get("/api/history").json["total"], 0)
        self.assertEqual(self.post("/api/browser-preview", {"browser_body": "{unknown}"}).status_code, 400)
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/browser-events").status_code, 401)
        self.assertEqual(self.client.post("/api/browser-preview", json={}).status_code, 403)

    def insert_browser_alert(self, stamp, bar_time, desktop=True):
        with connect(self.db) as c:
            return c.execute("""INSERT INTO alerts(rule_id,rule_name,symbol,stock_name,period,bar_time,
                title,body,values_json,created_at,browser_title,browser_body) VALUES (1,'test','sh688605','先锋精科','5',?,
                '规则标题','规则正文','{}',?,?,?)""", (bar_time, stamp.isoformat(), "桌面标题" if desktop else None, "桌面正文" if desktop else None)).lastrowid

    def test_browser_events_baseline_expiry_order_and_switch(self):
        config = self.client.get("/api/state").json["settings"]
        self.put("/api/settings", {**config, "browser_enabled": True})
        now = now_cn()
        old_id = self.insert_browser_alert(now - timedelta(minutes=6), "old")
        first = self.insert_browser_alert(now, "current-1")
        baseline = self.client.get("/api/browser-events").json
        self.assertEqual(baseline["items"], [])
        self.assertEqual(baseline["cursor"], first)
        second = self.insert_browser_alert(now, "current-2")
        bark_only = self.insert_browser_alert(now, "bark-only", False)
        events = self.client.get(f"/api/browser-events?after={old_id}").json
        self.assertEqual([item["id"] for item in events["items"]], [first, second])
        self.assertEqual(events["items"][0]["title"], "桌面标题")
        self.assertEqual(events["cursor"], bark_only)
        self.assertEqual(self.client.get(f"/api/browser-events?after={bark_only}").json["items"], [])
        self.assertEqual(self.client.get("/api/browser-events?after=-1").status_code, 400)
        self.put("/api/settings", {**config, "browser_enabled": False})
        self.assertFalse(self.client.get("/api/browser-events?after=0").json["enabled"])
        self.assertEqual(self.client.get("/api/browser-events?after=0").json["items"], [])

    def test_browser_events_pagination_does_not_skip(self):
        config = self.client.get("/api/state").json["settings"]
        self.put("/api/settings", {**config, "browser_enabled": True})
        for i in range(102): self.insert_browser_alert(now_cn(), str(i))
        first = self.client.get("/api/browser-events?after=0").json
        second = self.client.get(f"/api/browser-events?after={first['cursor']}").json
        self.assertEqual(len(first["items"]), 100)
        self.assertEqual(len(second["items"]), 2)

    def test_browser_schema_upgrade_preserves_data(self):
        alert_id = self.insert_browser_alert(now_cn(), "legacy")
        with connect(self.db) as c:
            c.execute("ALTER TABLE alerts DROP COLUMN browser_title")
            c.execute("ALTER TABLE alerts DROP COLUMN browser_body")
            old = {k: v for k, v in settings(c).items() if not k.startswith("browser_")}
            c.execute("UPDATE settings SET value=?", (json.dumps(old),))
        init_db(self.db)
        init_db(self.db)
        with connect(self.db) as c:
            alert = c.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
            self.assertEqual(alert["title"], "规则标题")
            self.assertIsNone(alert["browser_title"])
            self.assertFalse(settings(c)["browser_enabled"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM rules").fetchone()[0], 2)


class EngineTest(unittest.TestCase):
    setUp = AppTest.setUp
    tearDown = AppTest.tearDown
    post = AppTest.post
    add_target = AppTest.add_target
    def prepare(self, mode="on_enter", cooldown=0):
        self.target=self.add_target()
        self.now=datetime(2026,11,12,10,6,tzinfo=SHANGHAI)
        self.sender=Mock()
        self.engine=Engine(self.db,sender=self.sender,clock=lambda:self.now)
        with connect(self.db) as c:
            c.execute("UPDATE rules SET mode=?,cooldown_minutes=? WHERE id=1",(mode,cooldown))
            self.rule=decode(c.execute("SELECT * FROM rules WHERE id=1").fetchone(),("condition","periods","stock_ids","target_ids"))
            self.stock=decode(c.execute("SELECT * FROM stocks WHERE id=1").fetchone(),("periods",))
            self.config=settings(c)
        self.current={**bar(),"K":80.0,"D":60.0,"J":120.0,"RSV":90.0}
        self.previous={**bar("2026-11-12 09:55"),"K":55.0,"D":60.0,"J":45.0,"RSV":50.0}

    def evaluate(self,current=None):
        self.engine.evaluate(self.rule,self.stock,"5",current or self.current,self.previous,self.config,self.now)

    def count(self):
        with connect(self.db) as c: return c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]

    def test_first_signal_dedup_across_restart_and_reentry(self):
        self.prepare()
        self.evaluate(); self.evaluate()
        self.assertEqual(self.count(),1)
        self.engine.retry_deliveries(self.now.timestamp())
        self.assertEqual(self.sender.call_count,1)
        self.engine=Engine(self.db,sender=self.sender,clock=lambda:self.now)
        self.evaluate({**self.current,"time":"2026-11-12 10:05"})
        self.assertEqual(self.count(),1)
        self.evaluate({**self.previous,"time":"2026-11-12 10:10"})
        self.evaluate({**self.current,"time":"2026-11-12 10:15"})
        self.assertEqual(self.count(),2)

    def test_per_bar_cooldown(self):
        self.prepare("per_bar",10)
        self.evaluate()
        self.now+=timedelta(minutes=5)
        self.evaluate({**self.current,"time":"2026-11-12 10:05"})
        self.assertEqual(self.count(),1)
        self.now+=timedelta(minutes=5)
        self.evaluate({**self.current,"time":"2026-11-12 10:10"})
        self.assertEqual(self.count(),2)

    def test_partial_success_retry_only_failed_target(self):
        self.prepare()
        self.add_target("iPad")
        self.sender.side_effect=[None,ValueError("网络中断"),None]
        self.evaluate()
        epoch=self.now.timestamp()
        self.engine.retry_deliveries(epoch)
        self.assertEqual(self.sender.call_count,2)
        self.engine.retry_deliveries(epoch+30)
        self.assertEqual(self.sender.call_count,2)
        self.engine.retry_deliveries(epoch+61)
        self.assertEqual(self.sender.call_count,3)
        with connect(self.db) as c:
            self.assertEqual([r[0] for r in c.execute("SELECT status FROM deliveries")],["sent","sent"])

    def test_failed_three_times_stops_retry(self):
        self.prepare()
        self.sender.side_effect=ValueError("网络中断")
        self.evaluate()
        epoch=self.now.timestamp()
        for delay in (0,61,362,1000): self.engine.retry_deliveries(epoch+delay)
        self.assertEqual(self.sender.call_count,3)
        with connect(self.db) as c: self.assertEqual(c.execute("SELECT status FROM deliveries").fetchone()[0],"failed")

    def test_disabled_rule_cancels_pending_delivery(self):
        self.prepare()
        self.evaluate()
        with connect(self.db) as c: c.execute("UPDATE rules SET enabled=0 WHERE id=1")
        self.engine.retry_deliveries(self.now.timestamp())
        self.sender.assert_not_called()
        with connect(self.db) as c:
            self.assertEqual(c.execute("SELECT status FROM deliveries").fetchone()[0],"failed")

    def test_successful_scan_persists_real_computed_chart(self):
        self.prepare()
        market=Mock()
        def fetch(symbol, period, provider):
            delta=timedelta(days=1) if period == "day" else timedelta(minutes=int(period))
            bars=[bar((self.now - delta * (160-i)).strftime("%Y-%m-%d" if period == "day" else "%Y-%m-%d %H:%M"), 100+i) for i in range(160)]
            return bars,"先锋精科","fake"
        market.fetch.side_effect=fetch
        self.engine.market=market
        self.assertTrue(self.engine.run_once(manual=True))
        snapshots=self.client.get("/api/state").json["snapshots"]
        self.assertEqual(len(snapshots),3)
        self.assertTrue(all(len(row["data"]["history"]) == 48 for row in snapshots))
        self.assertTrue(all(row["data"]["kdj_n"] == 9 for row in snapshots))

    def test_no_target_no_consumed_edge(self):
        self.prepare()
        with connect(self.db) as c: c.execute("UPDATE targets SET enabled=0")
        self.evaluate()
        self.assertEqual(self.count(),0)
        with connect(self.db) as c: c.execute("UPDATE targets SET enabled=1")
        self.evaluate()
        self.assertEqual(self.count(),1)

    def test_browser_only_alert_and_template_snapshot(self):
        self.prepare()
        with connect(self.db) as c:
            c.execute("DELETE FROM targets")
            config = {**settings(c), "browser_enabled": True, "browser_template_mode": "custom",
                      "browser_title": "桌面 {name}", "browser_body": "{period} J={J} / {rule}"}
            c.execute("UPDATE settings SET value=?", (json.dumps(config),))
        # The engine's scan copy is older; the enabled channel is read atomically.
        self.evaluate()
        self.evaluate()
        self.assertEqual(self.count(), 1)
        self.engine.retry_deliveries(self.now.timestamp())
        self.sender.assert_not_called()
        with connect(self.db) as c:
            alert = c.execute("SELECT * FROM alerts").fetchone()
            self.assertEqual(alert["browser_title"], "桌面 先锋精科")
            self.assertIn("5分钟 J=120.00", alert["browser_body"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
            self.assertGreater(c.execute("SELECT last_sent FROM rule_state").fetchone()[0], 0)

    def test_browser_and_bark_share_one_trigger_follow_rule_content(self):
        self.prepare()
        with connect(self.db) as c:
            c.execute("UPDATE settings SET value=?", (json.dumps({**settings(c), "browser_enabled": True}),))
        self.evaluate()
        self.engine.retry_deliveries(self.now.timestamp())
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.sender.call_count, 1)
        with connect(self.db) as c:
            alert = c.execute("SELECT * FROM alerts").fetchone()
            self.assertEqual(alert["browser_title"], alert["title"])
            self.assertEqual(alert["browser_body"], alert["body"])

    def test_outdated_rule_copy_is_ignored(self):
        self.prepare()
        with connect(self.db) as c: c.execute("UPDATE rules SET enabled=0 WHERE id=1")
        self.evaluate()
        self.assertEqual(self.count(),0)

    def test_market_errors_and_stale_data_never_push(self):
        self.prepare()
        market=Mock()
        market.fetch.side_effect=ValueError("行情不可用")
        self.engine.market=market
        self.engine.run_once(manual=True)
        response=self.client.get("/api/state")
        self.assertEqual(len(response.json["snapshots"]),3)
        self.assertTrue(all(s["error"] for s in response.json["snapshots"]))
        old_bars=[bar((self.now-timedelta(days=180-i)).strftime("%Y-%m-%d"),10+i) for i in range(160)]
        market.fetch.side_effect=None
        market.fetch.return_value=(old_bars,"先锋精科","fake")
        self.engine.run_once(manual=True)
        self.assertEqual(self.count(),0)
        self.assertTrue(all(s["data"]["stale"] for s in self.client.get("/api/state").json["snapshots"]))


class BarkTest(unittest.TestCase):
    @patch("app.bark.requests.post")
    def test_post_payload_and_success(self,post):
        target={"server":"https://api.day.app","token":"hidden-key","group_name":"股票","sound":"bell","level":"active"}
        post.return_value.status_code=200
        post.return_value.json.return_value={"code":200}
        send_bark(target,"标题","正文")
        args,kwargs=post.call_args
        self.assertEqual(args[0],"https://api.day.app/push")
        self.assertEqual(kwargs["json"]["device_key"],"hidden-key")
        self.assertFalse(kwargs["allow_redirects"])
        post.return_value.status_code=400
        with self.assertRaises(ValueError): send_bark(target,"标题","正文")


if __name__ == "__main__": unittest.main()
