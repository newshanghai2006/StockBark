"""Deterministic KDJ with a 50/50 seed and configurable smoothing."""

import math


FIELDS = ("J", "K", "D", "RSV", "close")
OPS = (">", ">=", "<", "<=", "==", "!=", "cross_above", "cross_below")


def kdj(bars, n=9, m1=3, m2=3):
    if not (2 <= n <= 120 and 1 <= m1 <= 60 and 1 <= m2 <= 60):
        raise ValueError("KDJ 参数超出范围")
    result = []
    k = d = 50.0
    # A full N-bar window is required. Values before it are deliberately absent.
    for i in range(n - 1, len(bars)):
        window = bars[i - n + 1:i + 1]
        highest = max(b["high"] for b in window)
        lowest = min(b["low"] for b in window)
        rsv = 50.0 if highest == lowest else (bars[i]["close"] - lowest) / (highest - lowest) * 100
        k += (rsv - k) / m1
        d += (k - d) / m2
        result.append({**bars[i], "RSV": rsv, "K": k, "D": d, "J": 3 * k - 2 * d})
    return result


def validate_condition(config):
    if not isinstance(config, dict) or config.get("logic") not in ("all", "any"):
        raise ValueError("条件组合必须为全部满足或任一满足")
    items = config.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 12:
        raise ValueError("请设置 1～12 个条件")
    normalized = []
    for item in items:
        if not isinstance(item, dict) or item.get("left") not in FIELDS or item.get("op") not in OPS:
            raise ValueError("条件字段或比较运算符无效")
        right = item.get("right")
        if not (isinstance(right, str) and right in FIELDS):
            if isinstance(right, bool):
                raise ValueError("比较值必须为数字或指标")
            try:
                right = float(right)
            except (ValueError, TypeError):
                raise ValueError("比较值必须为数字或指标") from None
            if not math.isfinite(right) or abs(right) > 1e12:
                raise ValueError("比较值超出范围")
        normalized.append({"left": item["left"], "op": item["op"], "right": right})
    return {"logic": config["logic"], "items": normalized}


def matches(config, current, previous=None):
    def one(item):
        left = current[item["left"]]
        right = current[item["right"]] if isinstance(item["right"], str) else item["right"]
        op = item["op"]
        if op == ">": return left > right
        if op == ">=": return left >= right
        if op == "<": return left < right
        if op == "<=": return left <= right
        if op == "==": return left == right
        if op == "!=": return left != right
        if previous is None: return False
        old_left = previous[item["left"]]
        old_right = previous[item["right"]] if isinstance(item["right"], str) else item["right"]
        if op == "cross_above": return old_left <= old_right and left > right
        if op == "cross_below": return old_left >= old_right and left < right
        return False

    return (all if config["logic"] == "all" else any)(one(item) for item in config["items"])
