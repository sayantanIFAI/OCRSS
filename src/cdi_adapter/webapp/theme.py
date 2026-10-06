"""CareFlow Polyclinic look: white + blue, inline-SVG logo, system fonts, no build step, no CDN
(ARCHITECTURE §7). Palette tokens, type scale, spacing, component styles and the shared header."""

from __future__ import annotations

from html import escape

LOGO_SVG = (
    '<svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">'
    '<rect width="28" height="28" rx="7" fill="#1d4ed8"/>'
    '<path d="M14 7v14M7 14h14" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/></svg>'
)

CSS = """
:root{--bg:#ffffff;--surface:#f5f8ff;--border:#dbe4f5;--text:#0f1b33;--muted:#5b6b88;
--brand:#1d4ed8;--brand-weak:#e8efff;--ok:#157347;--ok-weak:#e7f6ee;--warn:#9a6700;
--warn-weak:#fff6dd;--bad:#b42318;--bad-weak:#fdecea;--radius:10px;--gap:16px;
--font:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans",Arial,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 var(--font)}
header.top{display:flex;align-items:center;gap:10px;padding:12px 20px;border-bottom:1px solid var(--border)}
header.top b{font-size:17px}header.top nav{margin-left:auto;display:flex;gap:14px}
header.top a{color:var(--brand);text-decoration:none}main{padding:20px;max-width:1280px;margin:0 auto}
.card{border:1px solid var(--border);border-radius:var(--radius);padding:var(--gap);background:#fff}
.muted{color:var(--muted)}.grid{display:grid;gap:var(--gap)}
button{font:inherit;border:1px solid var(--border);border-radius:8px;padding:6px 12px;background:#fff;cursor:pointer}
button.primary{background:var(--brand);border-color:var(--brand);color:#fff}
button.ok{background:var(--ok);border-color:var(--ok);color:#fff}
button.bad{background:var(--bad);border-color:var(--bad);color:#fff}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--brand-weak);color:var(--brand)}
.pill.warn{background:var(--warn-weak);color:var(--warn)}.pill.bad{background:var(--bad-weak);color:var(--bad)}
.pill.ok{background:var(--ok-weak);color:var(--ok)}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border)}
input,select,textarea{font:inherit;padding:6px 8px;border:1px solid var(--border);border-radius:8px}
"""


def header(active: str = "") -> str:
    links = [("/", "Generate EMR"), ("/review", "Review"), ("/reviewer", "Reviewer"),
             ("/admin", "Admin")]  # fmt: skip
    nav = "".join(
        f'<a href="{h}"{" aria-current=page" if h == active else ""}>{escape(t)}</a>'
        for h, t in links
    )
    return f'<header class="top">{LOGO_SVG}<b>CareFlow Polyclinic</b><nav>{nav}</nav></header>'


def page(title: str, body: str, *, active: str = "", script: str = "") -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{CSS}</style></head><body>"
        f"{header(active)}<main>{body}</main>"
        f"<script>{script}</script></body></html>"
    )
