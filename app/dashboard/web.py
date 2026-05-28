from __future__ import annotations

from importlib.resources import files


def _load_dashboard_html() -> str:
    return files("app.dashboard.static").joinpath("index.html").read_text(encoding="utf-8")


DASHBOARD_HTML = _load_dashboard_html()
