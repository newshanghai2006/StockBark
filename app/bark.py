from string import Formatter
from urllib.parse import urlsplit

import requests


TEMPLATE_FIELDS = {"symbol", "code", "name", "period", "rule", "J", "K", "D", "RSV", "close", "bar_time", "time"}


def validate_template(text, limit=2000):
    if not isinstance(text, str) or not text.strip() or len(text) > limit:
        raise ValueError(f"推送模板不能为空且最多 {limit} 字")
    try:
        for _, field, spec, conversion in Formatter().parse(text):
            if field is not None and (field not in TEMPLATE_FIELDS or spec or conversion):
                raise ValueError("模板仅支持页面列出的变量，不支持格式表达式")
    except ValueError:
        raise ValueError("模板格式无效，请使用 {J}、{name} 等变量") from None
    return text.strip()


def render_template(text, values):
    return text.format_map({key: f"{value:.2f}" if isinstance(value, float) else str(value) for key, value in values.items()})


def validate_server(raw):
    if not isinstance(raw, str):
        raise ValueError("Bark 服务地址无效")
    raw = raw.strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Bark 服务地址应为 http(s) 地址，例如 https://api.day.app")
    return raw


def send_bark(target, title, body):
    payload = {"device_key": target["token"], "title": title, "body": body,
               "group": target["group_name"], "level": target["level"], "isArchive": "1"}
    if target["sound"]:
        payload["sound"] = target["sound"]
    try:
        response = requests.post(target["server"] + "/push", json=payload, timeout=(5, 12), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f"Bark 返回 HTTP {response.status_code}，请检查服务地址和设备 Key")
        result = response.json()
        if not isinstance(result, dict) or result.get("code") != 200:
            raise ValueError("Bark 未确认发送成功，请检查设备 Key")
    except requests.RequestException:
        raise ValueError("无法连接 Bark 服务，请检查网络和服务地址") from None
    except (TypeError, requests.exceptions.JSONDecodeError):
        raise ValueError("Bark 返回内容无效，请检查服务地址") from None
