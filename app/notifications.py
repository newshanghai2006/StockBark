"""Browser content is rendered at trigger time, just like Bark content."""

from .bark import render_template, validate_template


def validate_browser_settings(data, defaults):
    enabled = data.get("browser_enabled", defaults["browser_enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("浏览器通知开关必须为布尔值")
    mode = data.get("browser_template_mode", defaults["browser_template_mode"])
    if mode not in ("rule", "custom"):
        raise ValueError("浏览器通知模板方式无效")
    return {
        "browser_enabled": enabled,
        "browser_template_mode": mode,
        "browser_title": validate_template(data.get("browser_title", defaults["browser_title"]), 200),
        "browser_body": validate_template(data.get("browser_body", defaults["browser_body"])),
    }


def browser_content(config, title, body, values):
    if config["browser_template_mode"] == "rule":
        return title, body
    return render_template(config["browser_title"], values), render_template(config["browser_body"], values)
