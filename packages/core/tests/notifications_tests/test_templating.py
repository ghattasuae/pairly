# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Template sandbox, variable types, escaping, lint (em dash, undeclared) and defaults."""

from __future__ import annotations

from typing import Any

import pytest

from pairly_core.notifications.defaults import ALL_SYSTEM_TEMPLATES as SYSTEM_TEMPLATES
from pairly_core.notifications.templating import (
    EMAIL_ACTION_BG,
    MAX_OUTPUT_CHARS,
    UNSUBSCRIBE_SLOT,
    TemplateRenderError,
    TemplateSource,
    TemplateVariableError,
    check_syntax,
    coerce_variables,
    fill_unsubscribe,
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


# ------------------------------------------------------------------ the email shell

_LINK_VARS: dict[str, Any] = {
    "name": {"type": "string"},
    "note": {"type": "text", "required": False},
    "open_url": {"type": "url"},
    "later_url": {"type": "url", "required": False},
}
_LINK_BODY = (
    "Hi {{ name }}.\n{% if note %}{{ note }}\n{% endif %}\nOpen your scorecard: {{ open_url }}"
    "{% if later_url %}\n\nNot now? Remind me later: {{ later_url }}{% endif %}"
)


def _email(locale: str = "en", **values: Any) -> str:
    src = _src(_LINK_BODY, locale=locale, variables=_LINK_VARS)
    rendered = render_source(
        src,
        {"name": "Sam", "open_url": "https://app.example/s?a=1&b=2", **values},
        org_name="Acme & Sons",
    )
    assert rendered.html is not None
    return rendered.html


def test_email_shell_structure() -> None:
    html = _email(later_url="https://app.example/later")
    # Table based, 600px wide at most, paper outside, a hairline card, no scripts or images.
    assert html.startswith('<!doctype html><html lang="en" dir="ltr">')
    assert 'role="presentation"' in html
    assert "max-width:600px" in html
    assert "border:1px solid" in html
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "url(" not in html
    assert "@import" not in html
    # Dark mode friendly.
    assert '<meta name="color-scheme" content="light dark">' in html
    assert '<meta name="supported-color-schemes" content="light dark">' in html
    assert "prefers-color-scheme:dark" in html
    # The organisation heads it, escaped, and the footer says why the email came.
    assert "Acme &amp; Sons" in html
    assert "Acme &amp; Sons sent you this email through Pairly." in html
    assert "Acme & Sons" not in html
    # The title uses the serif stack with safe fallbacks, the body the sans stack.
    assert "'Fraunces', Georgia, 'Times New Roman', serif" in html
    assert "'IBM Plex Sans', -apple-system, 'Segoe UI', Roboto, Arial, sans-serif" in html
    # The preheader carries the first line of the text, hidden.
    assert "mso-hide:all" in html
    assert ">Hi Sam.&#8203;" in html


def test_email_has_one_saffron_button_with_ink_text_and_escaped_links() -> None:
    html = _email(later_url="https://app.example/later")
    assert html.count(f'bgcolor="{EMAIL_ACTION_BG}"') == 1
    assert html.count('class="pl-btn-a"') == 1
    # The first declared link is the button, labelled by its line; the URL is escaped.
    assert 'href="https://app.example/s?a=1&amp;b=2"' in html
    assert ">Open your scorecard</a>" in html
    assert "a=1&b=2" not in html
    # Ink text on saffron, never white.
    assert "color:#ffffff" not in html.split('class="pl-btn-a"', 1)[1].split(">", 1)[0]
    # A later link stays a plain link.
    assert ">Not now? Remind me later</a>" in html


def test_outsider_text_never_becomes_a_link_or_button() -> None:
    html = _email(note="Pay now: https://evil.example/pay")
    assert 'href="https://evil.example' not in html
    assert "Pay now: https://evil.example/pay" in html
    assert html.count('class="pl-btn-a"') == 1


def test_email_without_links_has_no_button() -> None:
    src = _src("Hi {{ name }}")
    rendered = render_source(src, {"name": "Sam"})
    assert rendered.html is not None
    assert 'class="pl-btn-a"' not in rendered.html
    # No organisation known: the header falls back to the product name.
    assert ">Pairly</td>" in rendered.html


def test_custom_html_body_is_kept_and_framed() -> None:
    src = _src(
        "Hi {{ name }}",
        html='<p>Hi {{ name }} <a href="{{ u }}">open</a></p>',
        variables={"name": {"type": "string"}, "u": {"type": "url"}},
    )
    rendered = render_source(src, {"name": "<i>x</i>", "u": "https://a.example/?q=1&r=2"})
    assert rendered.html is not None
    assert '<p>Hi &lt;i&gt;x&lt;/i&gt; <a href="https://a.example/?q=1&amp;r=2">open</a></p>' in (
        rendered.html
    )
    assert 'class="pl-btn-a"' not in rendered.html


def test_arabic_email_is_rtl_throughout() -> None:
    html = _email("ar")
    assert html.startswith('<!doctype html><html lang="ar" dir="rtl">')
    assert '<body dir="rtl"' in html
    assert 'dir="ltr"' not in html
    assert 'align="left"' not in html
    assert "text-align:left" not in html
    assert 'align="right"' in html
    # Arabic copy, no Latin capitals or letter spacing, the Arabic sans for the title.
    assert "أرسلت Acme &amp; Sons إليك هذه الرسالة عبر Pairly." in html
    assert "text-transform:uppercase" not in html
    assert "'IBM Plex Sans Arabic'" in html


def test_plain_text_part_is_unchanged_and_complete() -> None:
    src = _src(_LINK_BODY, variables=_LINK_VARS)
    rendered = render_source(
        src,
        {"name": "Sam", "open_url": "https://app.example/s", "later_url": "https://app.example/l"},
        org_name="Acme",
    )
    assert rendered.text == (
        "Hi Sam.\n\nOpen your scorecard: https://app.example/s\n\n"
        "Not now? Remind me later: https://app.example/l"
    )


def test_render_is_deterministic_for_approval_hashes() -> None:
    assert _email(later_url="https://app.example/later") == _email(
        later_url="https://app.example/later"
    )


def test_unsubscribe_slot_is_filled_in_the_footer_only() -> None:
    html = _email()
    assert html.count(UNSUBSCRIBE_SLOT) == 1
    assert fill_unsubscribe(html, None, "en") == html
    filled = fill_unsubscribe(html, "https://api.example/u?token=a&b", "en")
    assert filled is not None
    assert UNSUBSCRIBE_SLOT not in filled
    assert 'href="https://api.example/u?token=a&amp;b"' in filled
    assert "Unsubscribe from emails like this" in filled
    # A copy of the marker inside a body is never the one filled.
    body = f"<p>{UNSUBSCRIBE_SLOT}</p>"
    framed = render_source(
        _src("x", html=body, variables={"name": {"type": "string"}}), {"name": "a"}
    ).html
    filled = fill_unsubscribe(framed, "https://api.example/u", "ar")
    assert filled is not None
    assert f"<p>{UNSUBSCRIBE_SLOT}</p>" in filled
    assert "إلغاء الاشتراك في رسائل كهذه" in filled


# ------------------------------------------------------------------ approval email v2

# Template versions are immutable once seeded: version 1 by migration 0011, version 2 by
# migration 0036. A change to either HTML needs a new version and its own migration.
_PINNED_HTML = {
    (1, "en"): "118a417724de16187f30d1cdb5c60b172d2d2a9fd21927d9d55b18d81d354f61",
    (1, "ar"): "c30c0681c1f0c35374b28be93ecda7905c39757294c05b6426934d9cf38890d8",
    (2, "en"): "5c1b3536053a6f59ccc8387c6d3d5c2a459733f6075f47580b3f3e07eb2013e1",
    (2, "ar"): "69e3eb4ca27a8901c25d480c48e6f31e9607850bc4c83bed7f056f14b663451f",
}


def test_approval_email_versions_are_pinned() -> None:
    import hashlib

    from pairly_core.notifications.defaults import APPROVAL_REQUESTED, APPROVAL_REQUESTED_V2

    for version, rows in ((1, APPROVAL_REQUESTED[:2]), (2, APPROVAL_REQUESTED_V2)):
        for t in rows:
            assert t.channel == "email"
            digest = hashlib.sha256((t.body_html or "").encode("utf-8")).hexdigest()
            assert digest == _PINNED_HTML[(version, t.locale)], (version, t.locale)
    # Version 2 keeps everything but the HTML.
    for v1, v2 in zip(APPROVAL_REQUESTED[:2], APPROVAL_REQUESTED_V2, strict=True):
        assert (v1.key, v1.channel, v1.locale, v1.category, v1.subject, v1.body, v1.variables) == (
            v2.key,
            v2.channel,
            v2.locale,
            v2.category,
            v2.subject,
            v2.body,
            v2.variables,
        )
        assert v2.requires_approval == v1.requires_approval


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_approval_email_v2_has_one_saffron_button_and_a_danger_outline(locale: str) -> None:
    from pairly_core.notifications.defaults import APPROVAL_REQUESTED_V2

    t = next(t for t in APPROVAL_REQUESTED_V2 if t.locale == locale)
    specs = parse_declarations(t.variables)
    src = TemplateSource(t.channel, t.locale, t.subject, t.body, t.body_html, specs)  # type: ignore[arg-type]
    assert lint_source(src) == []
    values = dict(sample_variables(specs))
    values["approve_url"] = "https://app.example/approvals/link?t=a&b=1#x"
    values["reject_url"] = "https://app.example/approvals/link?t=r&b=2#y"
    rendered = render_source(src, values, org_name="Acme")
    html = rendered.html
    assert html is not None
    assert html.count(f'bgcolor="{EMAIL_ACTION_BG}"') == 1
    assert html.count('class="pl-btn-a"') == 1
    assert html.count('class="pl-btn-danger-a"') == 1
    assert 'href="https://app.example/approvals/link?t=a&amp;b=1#x"' in html
    assert 'href="https://app.example/approvals/link?t=r&amp;b=2#y"' in html
    # No white text on a coloured fill anywhere.
    assert "#ffffff;text" not in html
    assert "color:#ffffff" not in html
    assert "<script" not in html.lower()
    assert f'dir="{"rtl" if locale == "ar" else "ltr"}"' in html
    # Without the one-time links there are no buttons at all.
    values.pop("approve_url")
    values.pop("reject_url")
    plain = render_source(src, values).html
    assert plain is not None
    assert "pl-btn" not in plain.split("</style>", 1)[1]
