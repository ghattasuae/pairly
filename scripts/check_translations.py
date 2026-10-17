# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Check that the product reads fully in English and Arabic (``make lint``).

Web messages (``apps/web/messages/en.json`` and ``ar.json``):

* every key in one file exists in the other (nested objects and arrays alike);
* no value is empty;
* no value has an em dash (U+2014; the house style, see ``check_no_em_dash``);
* the ICU arguments match: the same ``{names}`` in both languages (plural and select
  options may differ, Arabic has more plural forms), and a plural in one is a plural in
  the other.

Server-side copy:

* every notification template (``DefaultTemplate``, found in the modules that define
  them) exists in both ``en`` and ``ar`` for each key and channel, with a body (and a
  subject for email), and the same variables used in both;
* the offer letter (``pairly_hire.offers``): every PDF label and every offer template
  has its Arabic text.

Prints each problem and exits 1 when there is any.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MESSAGES = ROOT / "apps" / "web" / "messages"
LOCALES = ("en", "ar")
EM_DASH = "—"
PLURALS = {"plural", "selectordinal"}
# Modules that define notification templates (``DefaultTemplate``).
TEMPLATE_MODULES = (
    "pairly_core.notifications.defaults",
    "pairly_expense.templates",
    "pairly_hire.templates",
    "pairly_hire.messaging.library",
    "pairly_hire.portal.templates",
    "pairly_hire.portal.notices",
    "pairly_hire.scheduling.templates",
    "pairly_hire.interviewing.templates",
    "pairly_hire.references.templates",
    "pairly_hire.survey.templates",
    "pairly_pay.templates",
    "pairly_pay.portal.templates",
)
# ``{{ name }}`` and ``{% if name %}`` style variables in templates.
_TEMPLATE_VAR = re.compile(
    r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)|{%\s*(?:if|for [a-z_]+ in)\s+([a-zA-Z_][a-zA-Z0-9_]*)"
)


# ------------------------------------------------------------------ ICU


class IcuError(ValueError):
    pass


def icu_arguments(message: str) -> dict[str, str]:
    """The arguments of an ICU message, ``{name: type}`` (type ``""`` for a plain one)."""
    found: dict[str, str] = {}

    def text(i: int, nested: bool) -> int:
        while i < len(message):
            c = message[i]
            if c == "'" and i + 1 < len(message) and message[i + 1] in "{}'":
                # ICU quoting: '{' and '}' and '' are literal.
                end = message.find("'", i + 1)
                i = (end + 1) if end != -1 else len(message)
                continue
            if c == "{":
                i = argument(i + 1)
                continue
            if c == "}":
                if nested:
                    return i + 1
                raise IcuError("unbalanced }")
            i += 1
        if nested:
            raise IcuError("unclosed {")
        return i

    def argument(i: int) -> int:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*([,}])", message[i:])
        if match is None:
            raise IcuError(f"bad argument at {i}")
        name, sep = match.group(1), match.group(2)
        i += match.end()
        if sep == "}":
            found.setdefault(name, "")
            return i
        kind_match = re.match(r"\s*([a-z]+)\s*([,}])", message[i:])
        if kind_match is None:
            raise IcuError(f"bad argument type for {name}")
        kind, sep = kind_match.group(1), kind_match.group(2)
        i += kind_match.end()
        found[name] = kind
        if sep == "}":
            return i
        if kind in PLURALS or kind == "select":
            while True:
                option = re.match(r"\s*(offset:\s*\d+\s*)?([^\s{}]+)\s*{", message[i:])
                if option is None:
                    close = re.match(r"\s*}", message[i:])
                    if close is None:
                        raise IcuError(f"bad options for {name}")
                    return i + close.end()
                i = text(i + option.end(), True)
        # number, date, time with a style: skip to the matching brace.
        depth = 1
        while i < len(message) and depth:
            depth += {"{": 1, "}": -1}.get(message[i], 0)
            i += 1
        if depth:
            raise IcuError("unclosed {")
        return i

    text(0, False)
    return found


# ------------------------------------------------------------------ web messages


def flatten(value: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(value, list):
        for n, v in enumerate(value):
            yield from flatten(v, f"{prefix}[{n}]")
    else:
        yield prefix, value


def check_messages(directory: Path = MESSAGES) -> list[str]:
    problems: list[str] = []
    flat = {
        locale: dict(flatten(json.loads((directory / f"{locale}.json").read_text("utf-8"))))
        for locale in LOCALES
    }
    en, ar = flat["en"], flat["ar"]
    for key in sorted(en.keys() - ar.keys()):
        problems.append(f"messages: {key} is in en.json but not in ar.json")
    for key in sorted(ar.keys() - en.keys()):
        problems.append(f"messages: {key} is in ar.json but not in en.json")
    for locale, values in flat.items():
        for key, value in values.items():
            if not isinstance(value, str):
                problems.append(f"messages: {locale}.json {key} is not text")
            elif not value.strip():
                problems.append(f"messages: {locale}.json {key} is empty")
            elif EM_DASH in value:
                problems.append(f"messages: {locale}.json {key} has an em dash")
    for key in sorted(en.keys() & ar.keys()):
        a, b = en[key], ar[key]
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        try:
            args_en, args_ar = icu_arguments(a), icu_arguments(b)
        except IcuError as exc:
            problems.append(f"messages: {key} is not valid ICU ({exc})")
            continue
        if set(args_en) != set(args_ar):
            problems.append(
                f"messages: {key} placeholders differ: en {sorted(args_en)}, ar {sorted(args_ar)}"
            )
            continue
        for name in args_en:
            kinds = {args_en[name] in PLURALS, args_ar[name] in PLURALS}
            if len(kinds) > 1:
                problems.append(f"messages: {key} {{{name}}} is a plural in one language only")
    return problems


# ------------------------------------------------------------------ server-side copy


def _template_vars(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for text in texts:
        for match in _TEMPLATE_VAR.finditer(text or ""):
            out.add(match.group(1) or match.group(2))
    return out


def collect_templates(modules: tuple[str, ...] = TEMPLATE_MODULES) -> list[Any]:
    from pairly_core.notifications.defaults import DefaultTemplate

    found: dict[int, Any] = {}
    for name in modules:
        module = importlib.import_module(name)
        for value in vars(module).values():
            items = value if isinstance(value, tuple | list) else (value,)
            for item in items:
                if isinstance(item, DefaultTemplate):
                    found[id(item)] = item
    return list(found.values())


def check_templates(templates: list[Any]) -> list[str]:
    problems: list[str] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
    for t in templates:
        by_key[(t.key, t.channel)][t.locale] = t
    for (key, channel), locales in sorted(by_key.items()):
        for locale in LOCALES:
            if locale not in locales:
                problems.append(f"templates: {key} ({channel}) has no {locale} version")
        for locale, t in locales.items():
            if not t.body.strip():
                problems.append(f"templates: {key} ({channel}, {locale}) has an empty body")
            if channel == "email" and not (t.subject or "").strip():
                problems.append(f"templates: {key} ({channel}, {locale}) has no subject")
            for text in (t.body, t.subject, t.body_html):
                if text and EM_DASH in text:
                    problems.append(f"templates: {key} ({channel}, {locale}) has an em dash")
                    break
        if set(LOCALES) <= locales.keys():
            used = {loc: _template_vars(locales[loc].body, locales[loc].subject) for loc in LOCALES}
            if used["en"] != used["ar"]:
                problems.append(
                    f"templates: {key} ({channel}) variables differ: "
                    f"en {sorted(used['en'])}, ar {sorted(used['ar'])}"
                )
    return problems


def check_offer_letter() -> list[str]:
    from pairly_hire.offers.pdf import LABELS
    from pairly_hire.offers.templates import TEMPLATES

    problems: list[str] = []
    for key, (en, ar) in LABELS.items():
        if not en.strip() or not ar.strip():
            problems.append(f"offer letter: label {key} needs both languages")
    for t in TEMPLATES:
        pairs = (
            ("name", t.name, t.name_ar),
            ("opening", t.opening, t.opening_ar),
            ("closing", t.closing, t.closing_ar),
        )
        for field, en, ar in pairs:
            if not en.strip() or not ar.strip():
                problems.append(f"offer letter: template {t.key} {field} needs both languages")
        if len(t.conditions) != len(t.conditions_ar):
            problems.append(f"offer letter: template {t.key} conditions differ in number")
    return problems


def main() -> int:
    problems = [*check_messages(), *check_templates(collect_templates()), *check_offer_letter()]
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} translation problem(s)")
        return 1
    print("translations: en and ar match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
