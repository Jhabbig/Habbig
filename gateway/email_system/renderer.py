"""Tiny template engine for email templates.

Intentionally minimal — no Jinja2 dependency. Supports:

  - {{ variable }}          HTML-escaped substitution
  - {{ raw_variable }}      unescaped (match existing gateway render_page convention)
  - {% if var %} ... {% endif %}
  - {% for item in items %} ... {% endfor %}
  - {% extends "base.html" %}
  - {% block content %} ... {% endblock %}

Each child template extends base.html and defines a `content` block.
Variables come from the context dict passed at render time.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any


TEMPLATES_DIR = Path(__file__).parent / "templates"


def _load(name: str) -> str:
    path = TEMPLATES_DIR / f"{name}.html"
    if not path.exists():
        raise FileNotFoundError(f"email template not found: {name}")
    return path.read_text()


def _eval_expr(expr: str, ctx: dict) -> Any:
    """Evaluate a tiny expression against `ctx`.

    Supports `var`, `var.attr`, and literal strings. Intentionally does NOT
    execute arbitrary Python — safer against template-injection.
    """
    expr = expr.strip()
    if expr.startswith('"') and expr.endswith('"'):
        return expr[1:-1]
    if expr.startswith("'") and expr.endswith("'"):
        return expr[1:-1]
    parts = expr.split(".")
    cur: Any = ctx.get(parts[0])
    for p in parts[1:]:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            cur = getattr(cur, p, None)
    return cur


def _render_blocks(source: str, ctx: dict) -> str:
    """Replace `{% for %}` and `{% if %}` blocks and render variables inside
    their bodies against the loop/branch-local context.

    Called recursively for nested blocks. Variables inside the body must be
    resolved while the loop variable is in scope — once we exit the loop the
    child_ctx frame is gone, so we run `_render_vars` on each iteration.
    """
    # for loops
    def _for_sub(m: re.Match) -> str:
        var_name = m.group(1)
        iterable_name = m.group(2)
        body = m.group(3)
        items = _eval_expr(iterable_name, ctx) or []
        out = []
        for item in items:
            child_ctx = dict(ctx)
            child_ctx[var_name] = item
            rendered = _render_blocks(body, child_ctx)
            rendered = _render_vars(rendered, child_ctx)
            out.append(rendered)
        return "".join(out)

    source = re.sub(
        r"\{%\s*for\s+(\w+)\s+in\s+([\w\.]+)\s*%\}(.*?)\{%\s*endfor\s*%\}",
        _for_sub,
        source,
        flags=re.S,
    )

    # if blocks (truthy-only, no else)
    def _if_sub(m: re.Match) -> str:
        expr = m.group(1)
        body = m.group(2)
        val = _eval_expr(expr, ctx)
        if not val:
            return ""
        rendered = _render_blocks(body, ctx)
        rendered = _render_vars(rendered, ctx)
        return rendered

    source = re.sub(
        r"\{%\s*if\s+([\w\.]+)\s*%\}(.*?)\{%\s*endif\s*%\}",
        _if_sub,
        source,
        flags=re.S,
    )
    return source


def _render_vars(source: str, ctx: dict) -> str:
    def repl(m: re.Match) -> str:
        expr = m.group(1).strip()
        raw = expr.startswith("raw_")
        val = _eval_expr(expr, ctx)
        if val is None:
            return ""
        s = str(val)
        return s if raw else html.escape(s)

    return re.sub(r"\{\{\s*([\w\.]+)\s*\}\}", repl, source)


def render(template_name: str, context: dict) -> str:
    """Render a child template (extends base.html) with the given context.

    The child template defines `{% block content %}...{% endblock %}` which
    gets injected into base.html's `{{ content }}` placeholder.
    """
    child = _load(template_name)
    # Resolve content block.
    m = re.search(r"\{%\s*block\s+content\s*%\}(.*?)\{%\s*endblock\s*%\}", child, flags=re.S)
    if not m:
        # Templates without an extends block are used verbatim.
        content_block = child
        base = None
    else:
        content_block = m.group(1)
        base = _load("base")

    # Render the content block first.
    content_ctx = dict(context)
    rendered_content = _render_blocks(content_block, content_ctx)
    rendered_content = _render_vars(rendered_content, content_ctx)

    if base is None:
        return rendered_content

    base_ctx = dict(context)
    base_ctx["content"] = rendered_content  # content is raw HTML
    # Inject as raw_
    base_ctx["raw_content"] = rendered_content
    # Replace the base's `{{ content }}` explicitly with raw.
    base = base.replace("{{ content }}", rendered_content)
    base = _render_blocks(base, base_ctx)
    return _render_vars(base, base_ctx)


def render_text_fallback(html_content: str) -> str:
    """Strip HTML tags for the plain-text mime part."""
    text = re.sub(r"<br\s*/?>", "\n", html_content, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = html.unescape(text)
    return text.strip()
