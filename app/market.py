import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import requests


SHANGHAI = timezone(timedelta(hours=8))
PERIOD_NAMES = {"day": "日线", "5": "5分钟", "15": "15分钟"}
PROVIDERS = ("eastmoney", "tencent", "sina")
log = logging.getLogger(__name__)


def now_cn():
    return datetime.now(SHANGHAI)


def normalize_symbol(raw):
    text = str(raw).lower().strip().replace(".", "")
    match = re.fullmatch(r"(?:(sh|sz|bj))?(\d{6})", text)
    if not match:
        raise ValueError("请输入六位 A 股代码，例如 688605、sh688605")
    exchange, code = match.groups()
    if not exchange:
        exchange = "bj" if code.startswith("92") or code[0] in "48" else "sh" if code[0] in "569" else "sz"
    return exchange + code


def parse_time(raw):
    raw = str(raw)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y%m%d%H%M", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
    raise ValueError("行情时间格式无效")


def in_market(now):
    minute = now.hour * 60 + now.minute
    # Small grace period for the closing daily/minute candle to be published.
    return now.weekday() < 5 and (570 <= minute <= 692 or 780 <= minute <= 906)


def closed_bars(bars, period, now):
    result = []
    for bar in bars:
        stamp = parse_time(bar["time"])
        close_time = stamp.replace(hour=15, minute=1) if period == "day" else stamp + timedelta(seconds=3)
        if close_time <= now:
            result.append(bar)
    return result


def is_stale(bar, period, now):
    stamp = parse_time(bar["time"])
    if stamp.date() != now.date() or stamp > now:
        return True
    if period == "day":
        return False
    # Measure age in trading minutes, so the lunch break is not counted.
    age = 0.0
    for start_hour, start_minute, end_hour, end_minute in ((9, 30, 11, 30), (13, 0, 15, 0)):
        start = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        end = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        age += max(0, (min(now, end) - max(stamp, start)).total_seconds()) / 60
    return age > int(period) * 2 + 5


def clean_bars(bars):
    unique = {}
    for bar in bars:
        values = [bar[k] for k in ("open", "close", "high", "low")]
        if not all(math.isfinite(v) and v > 0 for v in values):
            continue
        if not bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]:
            continue
        dt = parse_time(bar["time"])
        bar["time"] = dt.strftime("%Y-%m-%d" if len(bar["time"]) <= 10 else "%Y-%m-%d %H:%M")
        unique[bar["time"]] = bar
    if not unique:
        raise ValueError("行情源未返回有效 K 线")
    return sorted(unique.values(), key=lambda b: b["time"])[-320:]


class MarketClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "*/*"})

    def fetch(self, symbol, period, provider="auto"):
        if provider != "auto" and provider not in PROVIDERS:
            raise ValueError("行情源无效")
        providers = PROVIDERS if provider == "auto" else (provider,)
        errors = []
        for name in providers:
            try:
                bars, stock_name = getattr(self, "_" + name)(symbol, period)
                return clean_bars(bars), stock_name, name
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
                # Do not leak upstream URLs/proxy credentials into user-visible errors.
                log.warning("Market fetch failed: provider=%s symbol=%s period=%s error=%s",
                            name, symbol, period, type(exc).__name__)
                errors.append(name)
        raise ValueError("行情获取失败（" + "、".join(errors) + "），请检查服务器网络或切换行情源")

    def _eastmoney(self, symbol, period):
        response = self.session.get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params={
            "secid": ("1." if symbol.startswith("sh") else "0.") + symbol[2:],
            "klt": "101" if period == "day" else period, "fqt": "0", "lmt": "320",
            "beg": "0", "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56",
        }, headers={"Referer": "https://quote.eastmoney.com/"}, timeout=(5, 12))
        response.raise_for_status()
        data = response.json()["data"]
        bars = []
        for line in data["klines"]:
            time, op, close, high, low, *_ = line.split(",")
            bars.append({"time": time, "open": float(op), "close": float(close), "high": float(high), "low": float(low)})
        return bars, data.get("name", "")

    def _tencent(self, symbol, period):
        if period == "day":
            url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            params = {"param": f"{symbol},day,,,320,"}
            key = "day"
        else:
            url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
            params = {"param": f"{symbol},m{period},,320"}
            key = f"m{period}"
        response = self.session.get(url, params=params, headers={"Referer": "https://gu.qq.com/"}, timeout=(5, 12))
        response.raise_for_status()
        data = response.json()["data"][symbol]
        bars = [{"time": str(row[0]), "open": float(row[1]), "close": float(row[2]), "high": float(row[3]), "low": float(row[4])} for row in data[key]]
        quote = data.get("qt", {}).get(symbol, [])
        return bars, quote[1] if len(quote) > 1 else ""

    def _sina(self, symbol, period):
        # Same JSONP endpoint as stocknotify. Its K lines are unadjusted,
        # matching the existing Eastmoney and Tencent price basis.
        response = self.session.get(
            "https://quotes.sina.cn/cn/api/jsonp_v2.php/IO.X=/CN_MarketDataService.getKLineData",
            params={"symbol": symbol, "scale": 240 if period == "day" else int(period),
                    "ma": "no", "datalen": 320},
            headers={"Referer": "https://finance.sina.com.cn/"}, timeout=(5, 12),
        )
        response.raise_for_status()
        text = response.text
        start, end = text.find("(["), text.rfind("])")
        if start < 0 or end < start:
            raise ValueError("新浪 K 线数据格式无效")
        rows = json.loads(text[start + 1:end + 1])
        bars = []
        for row in rows:
            stamp = parse_time(row["day"])
            bars.append({
                "time": stamp.strftime("%Y-%m-%d" if period == "day" else "%Y-%m-%d %H:%M"),
                **{key: float(row[key]) for key in ("open", "close", "high", "low")},
            })
        # Sina K lines do not include the company name; use the saved stock name.
        return bars, ""
