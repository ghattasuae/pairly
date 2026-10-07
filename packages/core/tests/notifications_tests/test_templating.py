# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Template sandbox, variable types, escaping, lint (em dash, undeclared) and defaults."""

from __future__ import annotations

from typing import Any

import pytest

from pairly_core.notifications.defaults import SYSTEM_TEMPLATES
from pairly_core.notifications.templating import (
    MAX_OUTPUT_CHARS,
    TemplateRenderError,
    TemplateSource,
    TemplateVariableError,
    check_syntax,
    coerce_variables,
    lint_source,
    parse_declarations,
    render_part,
    render_source,
    sample_variables,
)

EM = "—"


def _src(
    body: str,
    *,
    channel: str = "email",
    locale: str = "en",
    subject: str | None = "Hello {{ name }}",
    html: str | None = None,
    variables: dict[str, Any] | None = None,
) -> TemplateSource:
    specs = parse_declarations(variables if variables is not None else {"name": {"type": "string"}})
    return TemplateSource(channel, locale, subject, body, html, specs)  # type: ignore[arg-type]


def _codes(src: TemplateSource) -> set[str]:
    return {i.code for i in lint_source(src) if i.severity == "error"}


# ------------------------------------------------------------------ sandbox escapes


@pytest.mark.parametrize(
    "source",
    [
        "{{ name.__class__ }}",
        "{{ name.__class__.__mro__[1].__subclasses__() }}",
        "{{ items[0].__init__ }}",
        "{{ items[0]['__class__'] }}",
        "{{ name.upper() }}",
        "{{ name.format(1) }}",
        "{{ name|attr('__class__') }}",
        "{{ self }}",
        "{{ self._TemplateReference__context }}",
        "{{ cycler.__init__.__globals__ }}",
        "{{ lipsum.__globals__ }}",
        "{{ namespace() }}",
        "{% import 'os' as os %}",
        "{% from 'x' import y %}",
        "{% include 'secrets.txt' %}",
        "{% extends 'base.html' %}",
        "{% macro m() %}{{ m() }}{% endmacro %}{{ m() }}",
        "{% set x = name %}{{ x }}",
        "{% with x = name %}{{ x }}{% endwith %}",
        "{% filter upper %}x{% endfilter %}",
        "{% for i in items recursive %}{{ loop(items) }}{% endfor %}",
        "{{ 2 ** 64 }}",
    ],
)
def test_sandbox_refuses_escapes(source: str) -> None:
    values = {"name": "x", "items": [{"title": "a"}]}
    with pytest.raises(TemplateRenderError):
        render_part(source, "text", values)


@pytest.mark.parametrize(
    "source",
    [
        "{{ name.__class__ }}",
        "{{ name.upper() }}",
        "{% import 'os' as os %}",
        "{% include 'secrets.txt' %}",
        "{% extends 'base.html' %}",
        "{% set x = name %}",
        "{% for i in items recursive %}{% endfor %}",
        "{{ self }}",
    ],
)
def test_lint_catches_escapes_before_save(source: str) -> None:
    issues, _, _ = check_syntax(source, "text", "body")
    assert any(i.severity == "error" for i in issues)


def test_attribute_access_resolves_mapping_keys_only() -> None:
    values = {"items": [{"title": "Invoice 7"}], "name": "x"}
    assert render_part("{{ items[0].title }}", "text", values) == "Invoice 7"
    with pytest.raises(TemplateRenderError):
        render_part("{{ items[0].keys }}", "text", values)
    with pytest.raises(TemplateRenderError):
        render_part("{{ items.append }}", "text", values)


def test_loops_are_bounded() -> None:
    # range is capped.
    assert render_part("{% for i in range(3) %}{{ i }}{% endfor %}", "text", {}) == "012"
    with pytest.raises(TemplateRenderError, match="range"):
        render_part("{% for i in range(100000) %}x{% endfor %}", "text", {})
    with pytest.raises(TemplateRenderError, match="range"):
        render_part("{{ range(1000000000) }}", "text", {})
    # Nesting depth is capped (two loops), so work is at most 100 x 100 iterations.
    deep = (
        "{% for a in range(100) %}{% for b in range(100) %}{% for c in range(100) %}"
        "{% endfor %}{% endfor %}{% endfor %}"
    )
    with pytest.raises(TemplateRenderError, match="nest"):
        render_part(deep, "text", {})
    # Repetition and output size are capped.
    with pytest.raises(TemplateRenderError, match="repetition"):
        render_part("{{ name * 100000 }}", "text", {"name": "abc"})
    with pytest.raises(TemplateRenderError, match="too long"):
        render_part(
            "{% for a in range(100) %}{% for b in range(100) %}{{ name * 9 }}{% endfor %}"
            "{% endfor %}",
            "text",
            {"name": "x" * 1000},
        )
    assert MAX_OUTPUT_CHARS == 64 * 1024
    with pytest.raises(TemplateRenderError, match="numbers"):
        render_part("{{ '%999999999s' % name }}", "text", {"name": "x"})


def test_strict_undefined_and_optional_variables() -> None:
    src = _src(
        "Hi {{ name }}{% if note %} ({{ note }}){% endif %}",
        variables={"name": {"type": "string"}, "note": {"type": "string", "required": False}},
    )
    assert render_source(src, {"name": "Sam"}).text == "Hi Sam"
    assert render_source(src, {"name": "Sam", "note": "n"}).text == "Hi Sam (n)"
    with pytest.raises(TemplateVariableError, match="required"):
        render_source(src, {})
    with pytest.raises(TemplateRenderError):
        render_part("{{ missing }}", "text", {})


@pytest.mark.parametrize(
    ("kind", "bad"),
    [
        ("url", "javascript:alert(1)"),
        ("url", "data:text/html,x"),
        ("url", "https://x.test/a b"),
        ("email", "not-an-email"),
        ("integer", "3"),
        ("integer", True),
        ("decimal", "NaN"),
        ("boolean", "yes"),
        ("datetime", "2026-10-07T10:00:00"),
        ("list", "abc"),
        ("list", [object()]),
        ("string", "x" * 3000),
        ("string", "a\x00b"),
    ],
)
def test_variable_types_are_checked(kind: str, bad: Any) -> None:
    specs = parse_declarations({"v": {"type": kind}})
    with pytest.raises(TemplateVariableError):
        coerce_variables(specs, {"v": bad})


def test_render_refuses_a_loop_over_a_non_list_variable() -> None:
    src = _src("{% for c in name %}{{ c }}{% endfor %}")
    with pytest.raises(TemplateRenderError, match="list"):
        render_source(src, {"name": "x" * 2000})


def test_undeclared_values_are_ignored() -> None:
    specs = parse_declarations({"name": {"type": "string"}})
    assert coerce_variables(specs, {"name": "a", "other": "b"}) == {"name": "a"}


# ------------------------------------------------------------------ escaping


def test_html_is_autoescaped_and_text_is_not() -> None:
    src = _src(
        "Hi {{ name }}",
        html="<p>Hi {{ name }}</p>",
        variables={"name": {"type": "string"}},
    )
    rendered = render_source(src, {"name": "<script>alert(1)</script>"})
    assert rendered.text == "Hi <script>alert(1)</script>"
    assert rendered.html is not None
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered.html
    # Generated HTML (no body_html) is escaped too.
    plain = render_source(_src("Hi {{ name }}"), {"name": "<b>x</b>"})
    assert plain.html is not None
    assert "<b>x</b>" not in plain.html


def test_slack_text_is_escaped() -> None:
    src = _src("Hi {{ name }}", channel="slack", subject=None)
    rendered = render_source(src, {"name": "<!channel> & <@U123>"})
    assert rendered.text == "Hi &lt;!channel&gt; &amp; &lt;@U123&gt;"


def test_subject_is_one_line() -> None:
    src = _src("x", subject="Re: {{ name }}")
    rendered = render_source(src, {"name": "a\r\nBcc: victim@example.test"})
    assert rendered.subject is not None
    assert "\n" not in rendered.subject
    assert "\r" not in rendered.subject


def test_arabic_html_is_rtl() -> None:
    rendered = render_source(_src("مرحبا {{ name }}", locale="ar"), {"name": "سام"})
    assert rendered.html is not None
    assert '<html lang="ar" dir="rtl">' in rendered.html
    assert rendered.direction == "rtl"
    english = render_source(_src("Hi {{ name }}"), {"name": "Sam"})
    assert english.html is not None
    assert 'dir="ltr"' in english.html


# ------------------------------------------------------------------ lint


def test_em_dash_is_rejected_everywhere() -> None:
    assert "em_dash" in _codes(_src(f"Hello {EM} there {{{{ name }}}}"))
    assert "em_dash" in _codes(_src("Hello {{ name }}", subject=f"Hi {EM} you"))
    assert "em_dash" in _codes(_src("Hello {{ name }}", html="<p>a &mdash; b</p>"))
    assert "em_dash" in _codes(_src("Hello {{ name }}", html="<p>a &#8212; b</p>"))
    assert "em_dash" not in _codes(_src("Hello, {{ name }}: fine - plain hyphen."))


def test_lint_finds_undeclared_and_unused_variables() -> None:
    src = _src(
        "Hi {{ name }} {{ amount }}",
        variables={"name": {"type": "string"}, "spare": {"type": "string"}},
    )
    issues = lint_source(src)
    assert {"undeclared_variable"} <= {i.code for i in issues if i.severity == "error"}
    assert "unused_variable" in {i.code for i in issues if i.severity == "warning"}


def test_lint_structural_rules() -> None:
    assert "subject_required" in _codes(_src("Hi {{ name }}", subject=None))
    assert "html_email_only" in _codes(
        _src("Hi {{ name }}", channel="in_app", html="<p>{{ name }}</p>")
    )
    assert "loop_not_list" in _codes(_src("{% for i in name %}{{ i }}{% endfor %}", subject="s"))
    assert "syntax_error" in _codes(_src("{{ name ", subject="s"))
    assert "filter_not_allowed" in _codes(_src("{{ name|safe }}", subject="s"))


def test_declarations_are_validated() -> None:
    for bad in (
        {"Name": {"type": "string"}},
        {"x": {"type": "object"}},
        {"x": {"type": "string", "required": "yes"}},
        {"range": {"type": "string"}},
        {"x": {"type": "url", "sample": "javascript:x"}},
    ):
        with pytest.raises(TemplateRenderError):
            parse_declarations(bad)


def test_system_defaults_are_clean_and_render() -> None:
    keys = set()
    for t in SYSTEM_TEMPLATES:
        specs = parse_declarations(t.variables)
        src = TemplateSource(t.channel, t.locale, t.subject, t.body, t.body_html, specs)  # type: ignore[arg-type]
        assert lint_source(src) == [], (t.key, t.channel, t.locale)
        render_source(src, sample_variables(specs))
        keys.add((t.key, t.channel, t.locale))
    # Every default has both languages.
    for key, channel, _ in list(keys):
        assert (key, channel, "en") in keys
        assert (key, channel, "ar") in keys
    sensitive = {t.key for t in SYSTEM_TEMPLATES if t.requires_approval}
    assert sensitive == {"hire.candidate.rejection", "hire.candidate.offer", "pay.vendor.message"}
