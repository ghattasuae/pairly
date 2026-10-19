# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""Template language for notifications: a locked down Jinja2 subset.

Templates are written by organisation admins and rendered with values that can come
from outsiders (a candidate's name, an email subject). So rendering is sandboxed and
bounded, in layers:

1. ``ImmutableSandboxedEnvironment`` (no mutation of values), ``StrictUndefined`` (a
   missing variable is an error, never an empty string), autoescape for HTML bodies and
   Slack escaping (``& < >``) for Slack text.
2. Allowed syntax only (``check_syntax``, run on save and before every render): output,
   ``if``, ``for`` over a declared ``list`` variable or ``range(...)`` (at most two loops
   deep, never ``recursive``), filters from an allowlist, comparisons and arithmetic.
   Rejected: ``import``, ``include``, ``extends``, ``block``, ``macro``, ``call``,
   ``set``, ``with``, ``filter`` blocks, and every function or method call except
   ``range``. There is no loader, so even a smuggled ``include`` finds nothing.
3. Attribute access resolves mapping keys only (``item.title`` on a list item), never a
   Python attribute, and never a name starting with ``_``. ``loop`` exposes a few
   counters. So ``''.__class__`` and friends fail with ``SecurityError``.
4. Bounded work: ``range`` stops at 100, ``*`` may not build a string or list longer than
   10 000 items, ``**`` is refused, ``%`` is numbers only, list variables hold at most
   100 items and strings at most 2000 characters, and the output is cut off (an error)
   at 64 KiB. With loops at most two deep over bounded iterables, every render ends.

Variables are declared per template with a type (``string``, ``text``, ``url``,
``email``, ``integer``, ``decimal``, ``boolean``, ``datetime``, ``list``), a
``required`` flag and a ``sample`` value used by previews. Values are checked and
converted before rendering (``coerce_variables``); a ``url`` must be http(s), so a
``javascript:`` link can never reach an ``href``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import StrictUndefined, TemplateError, TemplateSyntaxError, meta, nodes
from jinja2.filters import FILTERS as DEFAULT_FILTERS
from jinja2.runtime import Context, LoopContext, Undefined
from jinja2.sandbox import ImmutableSandboxedEnvironment, SecurityError
from markupsafe import escape

Channel = Literal["email", "slack", "in_app"]
Locale = Literal["en", "ar"]
CHANNELS: Final = ("email", "slack", "in_app")
LOCALES: Final = ("en", "ar")
RTL_LOCALES: Final = frozenset({"ar"})

MAX_TEMPLATE_CHARS: Final = 20_000
MAX_SUBJECT_CHARS: Final = 200
MAX_OUTPUT_CHARS: Final = 64 * 1024
MAX_RANGE: Final = 100
MAX_SEQUENCE: Final = 10_000
MAX_LOOP_DEPTH: Final = 2
MAX_STRING: Final = 2000
MAX_TEXT: Final = 8000
MAX_LIST_ITEMS: Final = 100
MAX_VARIABLES: Final = 40

EM_DASH: Final = "—"
_EM_DASH_ENTITIES: Final = ("&mdash;", "&#8212;", "&#x2014;", "\\u2014")

VariableType = Literal[
    "string", "text", "url", "email", "integer", "decimal", "boolean", "datetime", "list"
]
VARIABLE_TYPES: Final = frozenset(
    {"string", "text", "url", "email", "integer", "decimal", "boolean", "datetime", "list"}
)
_VAR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")
_ITEM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

ALLOWED_FILTERS: Final = frozenset(
    {
        "upper",
        "lower",
        "title",
        "capitalize",
        "trim",
        "truncate",
        "default",
        "d",
        "length",
        "count",
        "round",
        "int",
        "abs",
        "first",
        "last",
        "escape",
        "e",
        "wordcount",
    }
)
_LOOP_ATTRIBUTES: Final = frozenset(
    {"index", "index0", "revindex", "revindex0", "first", "last", "length"}
)

_RESERVED_NAMES: Final = frozenset({"self", "caller", "varargs", "kwargs", "super"})
_ALLOWED_NODES: Final[tuple[type[nodes.Node], ...]] = (
    nodes.Template,
    nodes.Output,
    nodes.TemplateData,
    nodes.Const,
    nodes.Name,
    nodes.Getattr,
    nodes.Getitem,
    nodes.Slice,
    nodes.Filter,
    nodes.Test,
    nodes.If,
    nodes.For,
    nodes.CondExpr,
    nodes.Compare,
    nodes.Operand,
    nodes.And,
    nodes.Or,
    nodes.Not,
    nodes.Neg,
    nodes.Pos,
    nodes.Add,
    nodes.Sub,
    nodes.Mul,
    nodes.Div,
    nodes.FloorDiv,
    nodes.Mod,
    nodes.Concat,
    nodes.List,
    nodes.Tuple,
    nodes.Dict,
    nodes.Pair,
    nodes.Keyword,
    nodes.Call,
)


class TemplateRenderError(ValueError):
    """A template could not be rendered (bad syntax, bad value, a sandbox refusal)."""


class TemplateVariableError(TemplateRenderError):
    """A variable value is missing or does not match its declared type."""


# ------------------------------------------------------------------ declarations


@dataclass(frozen=True, slots=True)
class VariableSpec:
    name: str
    type: VariableType
    required: bool = True
    sample: Any = None
    description: str | None = None

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "required": self.required}
        if self.sample is not None:
            data["sample"] = self.sample
        if self.description:
            data["description"] = self.description
        return data


def parse_declarations(raw: Mapping[str, Any]) -> dict[str, VariableSpec]:
    """Validate a ``{name: {type, required, sample, description}}`` mapping."""
    if not isinstance(raw, Mapping):
        raise TemplateRenderError("variables must be an object")
    if len(raw) > MAX_VARIABLES:
        raise TemplateRenderError(f"at most {MAX_VARIABLES} variables")
    specs: dict[str, VariableSpec] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not _VAR_NAME_RE.fullmatch(name):
            raise TemplateRenderError(f"invalid variable name {name!r}")
        if name in {"range", "loop"}:
            raise TemplateRenderError(f"{name!r} is reserved")
        if not isinstance(value, Mapping):
            raise TemplateRenderError(f"variable {name}: expected an object")
        kind = value.get("type")
        if kind not in VARIABLE_TYPES:
            raise TemplateRenderError(f"variable {name}: unknown type {kind!r}")
        required = value.get("required", True)
        if not isinstance(required, bool):
            raise TemplateRenderError(f"variable {name}: required must be true or false")
        description = value.get("description")
        if description is not None and (not isinstance(description, str) or len(description) > 200):
            raise TemplateRenderError(f"variable {name}: description too long")
        spec = VariableSpec(name, kind, required, value.get("sample"), description)
        if spec.sample is not None:
            coerce_value(spec, spec.sample)  # a sample must itself be valid
        specs[name] = spec
    return specs


def declarations_json(specs: Mapping[str, VariableSpec]) -> dict[str, Any]:
    return {name: spec.to_json() for name, spec in sorted(specs.items())}


def _clean_string(name: str, value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise TemplateVariableError(f"{name}: expected a string")
    if "\x00" in value:
        raise TemplateVariableError(f"{name}: NUL characters are not allowed")
    if len(value) > limit:
        raise TemplateVariableError(f"{name}: longer than {limit} characters")
    return value


def coerce_value(spec: VariableSpec, value: object) -> Any:
    """Check ``value`` against ``spec`` and return the value templates see."""
    name = spec.name
    match spec.type:
        case "string":
            return _clean_string(name, value, MAX_STRING)
        case "text":
            return _clean_string(name, value, MAX_TEXT)
        case "url":
            text = _clean_string(name, value, MAX_STRING)
            parts = urlsplit(text)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.netloc
                or any(ch in text for ch in "\r\n\t <>\"'`")
            ):
                raise TemplateVariableError(f"{name}: expected an http(s) URL")
            return text
        case "email":
            text = _clean_string(name, value, 320)
            if not _EMAIL_RE.fullmatch(text):
                raise TemplateVariableError(f"{name}: expected an email address")
            return text
        case "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TemplateVariableError(f"{name}: expected an integer")
            if abs(value) > 10**15:
                raise TemplateVariableError(f"{name}: integer out of range")
            return value
        case "decimal":
            if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
                raise TemplateVariableError(f"{name}: expected a decimal string")
            try:
                number = Decimal(str(value))
            except InvalidOperation:
                raise TemplateVariableError(f"{name}: expected a decimal string") from None
            if not number.is_finite() or abs(number) > Decimal(10) ** 15:
                raise TemplateVariableError(f"{name}: decimal out of range")
            return str(number)
        case "boolean":
            if not isinstance(value, bool):
                raise TemplateVariableError(f"{name}: expected true or false")
            return value
        case "datetime":
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    raise TemplateVariableError(f"{name}: expected an ISO 8601 time") from None
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise TemplateVariableError(f"{name}: expected a time with a time zone")
            return value
        case "list":
            if not isinstance(value, list | tuple):
                raise TemplateVariableError(f"{name}: expected a list")
            if len(value) > MAX_LIST_ITEMS:
                raise TemplateVariableError(f"{name}: at most {MAX_LIST_ITEMS} items")
            items: list[Any] = []
            for item in value:
                if isinstance(item, str):
                    items.append(_clean_string(name, item, MAX_STRING))
                elif isinstance(item, Mapping):
                    if len(item) > 10:
                        raise TemplateVariableError(f"{name}: list items have at most 10 keys")
                    clean: dict[str, str] = {}
                    for key, field_value in item.items():
                        if not isinstance(key, str) or not _ITEM_KEY_RE.fullmatch(key):
                            raise TemplateVariableError(f"{name}: invalid item key")
                        clean[key] = _clean_string(name, field_value, MAX_STRING)
                    items.append(clean)
                else:
                    raise TemplateVariableError(f"{name}: list items are strings or objects")
            return items
    raise TemplateVariableError(f"{name}: unknown type")  # pragma: no cover


def coerce_variables(
    specs: Mapping[str, VariableSpec], values: Mapping[str, Any]
) -> dict[str, Any]:
    """The declared variables, checked. Values for undeclared names are ignored.

    An optional variable without a value is None: test it with ``{% if name %}``.
    """
    result: dict[str, Any] = {}
    for name, spec in specs.items():
        if name not in values or values[name] is None:
            if spec.required:
                raise TemplateVariableError(f"{name}: required")
            # Optional and absent: None, so ``{% if name %}`` works under StrictUndefined.
            result[name] = None
            continue
        result[name] = coerce_value(spec, values[name])
    return result


def sample_variables(specs: Mapping[str, VariableSpec]) -> dict[str, Any]:
    """Values for previews: each declared sample, or a placeholder of the right type."""
    placeholders: dict[str, Any] = {
        "string": "Sample",
        "text": "Sample text.",
        "url": "https://app.pairly.example/sample",
        "email": "person@example.test",
        "integer": 3,
        "decimal": "1250.00",
        "boolean": True,
        "datetime": "2026-10-07T09:30:00+00:00",
        "list": [{"title": "Sample item", "url": "https://app.pairly.example/sample"}],
    }
    return {
        name: spec.sample if spec.sample is not None else placeholders[spec.type]
        for name, spec in specs.items()
    }


# ------------------------------------------------------------------ environment


def _safe_range(*args: Any) -> range:
    if not 1 <= len(args) <= 3 or any(isinstance(a, bool) or not isinstance(a, int) for a in args):
        raise SecurityError("range takes one to three integers")
    rng = range(*args)
    if len(rng) > MAX_RANGE:
        raise SecurityError(f"range is limited to {MAX_RANGE} items")
    return rng


_MONTHS: Final[Mapping[str, tuple[str, ...]]] = {
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
    "ar": (
        "يناير",
        "فبراير",
        "مارس",
        "أبريل",
        "مايو",
        "يونيو",
        "يوليو",
        "أغسطس",
        "سبتمبر",
        "أكتوبر",
        "نوفمبر",
        "ديسمبر",
    ),
}


def format_datetime(value: datetime, locale: str, tz: str | None) -> str:
    """``7 Oct 2026, 13:30 (Asia/Dubai)``. Plain digits in every locale."""
    zone_name = tz or "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone, zone_name = ZoneInfo("UTC"), "UTC"
    local = value.astimezone(zone)
    month = _MONTHS.get(locale, _MONTHS["en"])[local.month - 1]
    return f"{local.day} {month} {local.year}, {local:%H:%M} ({zone_name})"


class _Sandbox(ImmutableSandboxedEnvironment):
    """See the module docstring, layer 3 and 4."""

    intercepted_binops = frozenset({"*", "**", "%"})

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        if operator == "**":
            raise SecurityError("the power operator is not allowed")
        if operator == "%":
            if not all(isinstance(v, int | float | Decimal) for v in (left, right)):
                raise SecurityError("% works on numbers only")
            return left % right
        # "*"
        for seq, times in ((left, right), (right, left)):
            if (
                isinstance(seq, str | list | tuple)
                and isinstance(times, int)
                and len(seq) * max(times, 0) > MAX_SEQUENCE
            ):
                raise SecurityError("repetition result too long")
        return left * right

    def getattr(self, obj: Any, attribute: str) -> Any:
        if not isinstance(attribute, str) or attribute.startswith("_"):
            raise SecurityError("access to private names is not allowed")
        if isinstance(obj, LoopContext):
            if attribute not in _LOOP_ATTRIBUTES:
                raise SecurityError(f"loop.{attribute} is not available")
            return getattr(obj, attribute)
        if isinstance(obj, Mapping):
            if attribute in obj:
                return obj[attribute]
            return self.undefined(obj=obj, name=attribute)
        if isinstance(obj, Undefined):
            return obj
        raise SecurityError("attribute access is only allowed on list items")

    def getitem(self, obj: Any, argument: Any) -> Any:
        if isinstance(obj, Mapping):
            if not isinstance(argument, str) or argument.startswith("_"):
                raise SecurityError("access to private names is not allowed")
            if argument in obj:
                return obj[argument]
            return self.undefined(obj=obj, name=argument)
        if isinstance(obj, str | list | tuple) and (
            isinstance(argument, slice)
            or (isinstance(argument, int) and not isinstance(argument, bool))
        ):
            try:
                return obj[argument]
            except IndexError:
                return self.undefined(obj=obj, name=str(argument))
        if isinstance(obj, Undefined):
            return obj
        raise SecurityError("subscript not allowed here")

    def call(__self, __context: Context, __obj: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: N805
        # Jinja itself passes ``_loop_vars`` / ``_block_vars`` to calls inside loops.
        if __obj is _safe_range and set(kwargs) <= {"_loop_vars", "_block_vars"}:
            return _safe_range(*args)
        raise SecurityError("calling functions or methods is not allowed")


def _slack_escape(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return value


Format = Literal["text", "html", "slack"]


def _make_env(fmt: Format) -> _Sandbox:
    env = _Sandbox(
        autoescape=fmt == "html",
        undefined=StrictUndefined,
        finalize=_slack_escape if fmt == "slack" else None,
        keep_trailing_newline=False,
        auto_reload=False,
        cache_size=0,
    )
    env.filters = {name: DEFAULT_FILTERS[name] for name in ALLOWED_FILTERS}
    env.globals = {"range": _safe_range}
    return env


_ENVS: Final[dict[Format, _Sandbox]] = {fmt: _make_env(fmt) for fmt in ("text", "html", "slack")}


# ------------------------------------------------------------------ syntax checks


@dataclass(frozen=True, slots=True)
class LintIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str
    field: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "field": self.field,
        }


def _walk(node: nodes.Node, loop_depth: int, issues: list[LintIssue], where: str) -> None:
    if not isinstance(node, _ALLOWED_NODES):
        issues.append(
            LintIssue("error", "syntax_not_allowed", f"{type(node).__name__} is not allowed", where)
        )
        return
    if isinstance(node, nodes.Call) and (
        not (isinstance(node.node, nodes.Name) and node.node.name == "range")
        or node.kwargs
        or node.dyn_args
        or node.dyn_kwargs
    ):
        issues.append(
            LintIssue("error", "call_not_allowed", "only range(...) may be called", where)
        )
        return
    if isinstance(node, nodes.Filter) and node.name not in ALLOWED_FILTERS:
        issues.append(
            LintIssue("error", "filter_not_allowed", f"filter {node.name!r} is not allowed", where)
        )
    name = node.attr if isinstance(node, nodes.Getattr) else getattr(node, "name", "")
    if isinstance(node, nodes.Getattr | nodes.Name) and str(name).startswith("_"):
        issues.append(
            LintIssue("error", "private_name", "names starting with _ are not allowed", where)
        )
        return
    if isinstance(node, nodes.Name) and node.name in _RESERVED_NAMES:
        issues.append(LintIssue("error", "reserved_name", f"{node.name!r} is not available", where))
        return
    depth = loop_depth
    if isinstance(node, nodes.For):
        if node.recursive:
            issues.append(
                LintIssue("error", "recursive_loop", "recursive loops are not allowed", where)
            )
            return
        depth += 1
        if depth > MAX_LOOP_DEPTH:
            issues.append(
                LintIssue(
                    "error", "loop_too_deep", f"loops nest at most {MAX_LOOP_DEPTH} deep", where
                )
            )
            return
        it = node.iter
        is_range = (
            isinstance(it, nodes.Call)
            and isinstance(it.node, nodes.Name)
            and it.node.name == "range"
        )
        if not (isinstance(it, nodes.Name) or is_range):
            issues.append(
                LintIssue(
                    "error",
                    "loop_iterable",
                    "a loop must go over a list variable or range(...)",
                    where,
                )
            )
            return
        if not isinstance(node.target, nodes.Name):
            issues.append(LintIssue("error", "loop_target", "loop over one name at a time", where))
            return
    for child in node.iter_child_nodes():
        _walk(child, depth, issues, where)


def check_syntax(
    source: str, fmt: Format, where: str
) -> tuple[list[LintIssue], set[str], set[str]]:
    """Syntax issues, the free variables, and the names used as list loop sources."""
    issues: list[LintIssue] = []
    if len(source) > MAX_TEMPLATE_CHARS:
        return [LintIssue("error", "too_long", "template too long", where)], set(), set()
    try:
        ast = _ENVS[fmt].parse(source)
    except TemplateSyntaxError as exc:
        return (
            [LintIssue("error", "syntax_error", f"line {exc.lineno}: {exc.message}", where)],
            set(),
            set(),
        )
    _walk(ast, 0, issues, where)
    if issues:
        return issues, set(), set()
    try:
        free = set(meta.find_undeclared_variables(ast)) - {"range"}
    except TemplateSyntaxError as exc:  # includes TemplateAssertionError (unknown filter)
        return [LintIssue("error", "syntax_error", exc.message or "invalid", where)], set(), set()
    loop_sources = {n.iter.name for n in ast.find_all(nodes.For) if isinstance(n.iter, nodes.Name)}
    return issues, free, loop_sources


def has_em_dash(text: str | None) -> bool:
    if not text:
        return False
    return EM_DASH in text or any(entity in text.lower() for entity in _EM_DASH_ENTITIES)


@dataclass(frozen=True, slots=True)
class TemplateSource:
    """What a template version holds (the parts that are rendered)."""

    channel: Channel
    locale: Locale
    subject: str | None
    body: str
    body_html: str | None
    variables: Mapping[str, VariableSpec] = field(default_factory=dict)


def lint_source(src: TemplateSource) -> list[LintIssue]:
    """Errors block saving; warnings are shown. Missing ``ar`` versions are checked by the
    template service, which can see the other versions."""
    issues: list[LintIssue] = []
    parts: list[tuple[str, str | None, Format]] = [
        ("subject", src.subject, "text"),
        ("body", src.body, "slack" if src.channel == "slack" else "text"),
        ("body_html", src.body_html, "html"),
    ]
    if src.channel == "email" and not (src.subject and src.subject.strip()):
        issues.append(
            LintIssue("error", "subject_required", "email templates need a subject", "subject")
        )
    if src.channel != "email" and src.body_html:
        issues.append(
            LintIssue("error", "html_email_only", "only email templates have HTML", "body_html")
        )
    if src.subject and ("\n" in src.subject or "\r" in src.subject):
        issues.append(LintIssue("error", "subject_one_line", "the subject is one line", "subject"))
    if not src.body.strip():
        issues.append(LintIssue("error", "body_required", "the body is empty", "body"))
    used: set[str] = set()
    list_sources: set[str] = set()
    for name, text, fmt in parts:
        if text is None:
            continue
        if has_em_dash(text):
            issues.append(
                LintIssue(
                    "error",
                    "em_dash",
                    "em dashes are not allowed by the copy style: use a comma, colon or full stop",
                    name,
                )
            )
        found, free, loops = check_syntax(text, fmt, name)
        issues.extend(found)
        used |= free
        list_sources |= loops
    for name in sorted(used - set(src.variables)):
        issues.append(
            LintIssue("error", "undeclared_variable", f"variable {name!r} is not declared", None)
        )
    for name in sorted(set(src.variables) - used):
        issues.append(
            LintIssue(
                "warning", "unused_variable", f"variable {name!r} is declared but unused", None
            )
        )
    for name in sorted(list_sources & set(src.variables)):
        if src.variables[name].type != "list":
            issues.append(
                LintIssue(
                    "error", "loop_not_list", f"loops need a list variable, {name!r} is not", None
                )
            )
    for spec in src.variables.values():
        if isinstance(spec.sample, str) and has_em_dash(spec.sample):
            issues.append(
                LintIssue("error", "em_dash", f"sample of {spec.name!r} has an em dash", None)
            )
    return issues


# ------------------------------------------------------------------ rendering


def _bounded(chunks: Iterator[str]) -> str:
    out: list[str] = []
    size = 0
    for chunk in chunks:
        size += len(chunk)
        if size > MAX_OUTPUT_CHARS:
            raise TemplateRenderError("rendered output too long")
        out.append(chunk)
    return "".join(out)


def render_part(
    source: str,
    fmt: Format,
    values: Mapping[str, Any],
    *,
    locale: str = "en",
    tz: str | None = None,
) -> str:
    """Render one part. Re-checks the syntax first, so stored templates can never bypass it."""
    issues, _, _ = check_syntax(source, fmt, "template")
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise TemplateRenderError(errors[0].message)
    prepared = {
        k: format_datetime(v, locale, tz) if isinstance(v, datetime) else v
        for k, v in values.items()
    }
    env = _ENVS[fmt]
    try:
        template = env.from_string(source)
        return _bounded(template.generate(prepared))
    except SecurityError as exc:
        raise TemplateRenderError(f"refused by the sandbox: {exc}") from None
    except TemplateRenderError:
        raise
    except TemplateError as exc:
        raise TemplateRenderError(str(exc.message or "template error")) from None
    except (TypeError, ValueError, ArithmeticError, OverflowError, RecursionError) as exc:
        raise TemplateRenderError(f"render failed: {type(exc).__name__}") from None


@dataclass(frozen=True, slots=True)
class Rendered:
    subject: str | None
    text: str
    html: str | None
    locale: str

    @property
    def direction(self) -> str:
        return "rtl" if self.locale in RTL_LOCALES else "ltr"


def _single_line(text: str) -> str:
    return " ".join(text.split())[:MAX_SUBJECT_CHARS]


# ------------------------------------------------------------------ the email shell
#
# Every email shares one layout: table based, inline styles only (a head style adds the
# dark-mode and phone overrides that clients without it simply skip), at most 600px wide,
# a warm paper background, a white card with a hairline border, the organisation name as
# a small header, the subject as the title, then the body and a muted footer. No images,
# no scripts and no web fonts are required: the stacks fall back to Georgia and the
# system sans. Colours come from the "ink and saffron" tokens of the web app, as hex.

_EMAIL_PAPER: Final = "#f4efe7"
_EMAIL_CARD: Final = "#ffffff"
_EMAIL_RULE: Final = "#e2dcd2"
_EMAIL_INK: Final = "#1a1511"
_EMAIL_MUTED: Final = "#635c56"
_EMAIL_LINK: Final = "#405870"
_EMAIL_DANGER: Final = "#a12721"
# Saffron with ink text on it: 7.0:1, never white text.
EMAIL_ACTION_BG: Final = "#e78c08"

_SERIF: Final = "'Fraunces', Georgia, 'Times New Roman', serif"
_SANS: Final = "'IBM Plex Sans', -apple-system, 'Segoe UI', Roboto, Arial, sans-serif"
_SANS_AR: Final = "'IBM Plex Sans Arabic', 'Segoe UI', Tahoma, Arial, sans-serif"

# Where the delivery step puts the one-click unsubscribe link (only it knows the signed
# URL, and only for mail people can turn off). An HTML comment, so it shows nothing when
# no link is filled in.
UNSUBSCRIBE_SLOT: Final = "<!--pairly:unsubscribe-->"

_EMAIL_COPY: Final[dict[str, dict[str, str]]] = {
    "en": {
        "why": "{org} sent you this email through Pairly.",
        "unsubscribe": "Unsubscribe from emails like this",
        "fallback": "If the button does not work, copy this link into your browser:",
        "open": "Open the link",
    },
    "ar": {
        "why": "أرسلت {org} إليك هذه الرسالة عبر Pairly.",
        "unsubscribe": "إلغاء الاشتراك في رسائل كهذه",
        "fallback": "إذا لم يعمل الزر، انسخ هذا الرابط في المتصفح:",
        "open": "افتح الرابط",
    },
}

_EMAIL_STYLE: Final = (
    "<style>"
    ":root{color-scheme:light dark;supported-color-schemes:light dark}"
    "body{margin:0;padding:0;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}"
    f".pl-body a{{color:{_EMAIL_LINK}}}"
    ".pl-body p{margin:0 0 16px}"
    "@media (max-width:620px){.pl-pad{padding-left:20px!important;padding-right:20px!important}"
    ".pl-title{font-size:24px!important;line-height:30px!important}}"
    "@media (prefers-color-scheme:dark){"
    ".pl-bg{background:#14110e!important}"
    ".pl-card{background:#1c1915!important;border-color:#312d28!important}"
    ".pl-ink,.pl-body{color:#ebe7df!important}"
    ".pl-muted{color:#a9a49c!important}"
    ".pl-rule{border-color:#312d28!important}"
    ".pl-body a,.pl-link{color:#95b5d6!important}"
    "a.pl-btn-a{color:#201308!important}"
    ".pl-btn{background:#f1aa4f!important}"
    ".pl-btn-danger{border-color:#f98f84!important}"
    "a.pl-btn-danger-a{color:#f98f84!important}}"
    "[data-ogsc] .pl-ink,[data-ogsc] .pl-body{color:#ebe7df!important}"
    "[data-ogsc] .pl-muted{color:#a9a49c!important}"
    "[data-ogsc] .pl-body a,[data-ogsc] .pl-link{color:#95b5d6!important}"
    "[data-ogsc] a.pl-btn-a{color:#201308!important}"
    "[data-ogsc] a.pl-btn-danger-a{color:#f98f84!important}"
    "</style>"
)


def _copy(locale: str) -> dict[str, str]:
    return _EMAIL_COPY.get(locale, _EMAIL_COPY["en"])


def _sans(locale: str) -> str:
    return _SANS_AR if locale in RTL_LOCALES else _SANS


def _saffron_cell(href: str, label: str, sans: str) -> str:
    return (
        f'<td class="pl-btn" align="center" bgcolor="{EMAIL_ACTION_BG}" '
        f'style="border-radius:6px;background:{EMAIL_ACTION_BG}">'
        f'<a class="pl-btn-a" href="{href}" target="_blank" rel="noopener" '
        f'style="display:inline-block;padding:12px 24px;border-radius:6px;font-family:{sans};'
        f"font-size:15px;font-weight:600;line-height:20px;color:{_EMAIL_INK};"
        f'text-decoration:none">{escape(label)}</a></td>'
    )


def email_button(url: str, label: str, locale: str = "en") -> str:
    """The one main action: a bulletproof saffron button (a padded link inside a coloured
    cell, so Outlook still shows the colour), with the link spelled out below it. The URL,
    the label and the copy are escaped; everything else is a constant."""
    href = escape(url)
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:separate;margin:8px 0 12px"><tr>'
        f"{_saffron_cell(str(href), label, _sans(locale))}</tr></table>"
        f'<p class="pl-muted" style="margin:0 0 16px;font-size:13px;line-height:20px;'
        f'color:{_EMAIL_MUTED}">{escape(_copy(locale)["fallback"])} '
        # Only the URL may break anywhere; the words around it wrap normally.
        f'<a class="pl-link" href="{href}" style="color:{_EMAIL_LINK};'
        f'word-break:break-all;overflow-wrap:anywhere">{href}</a></p>'
    )


def email_decision_buttons(
    approve_url: str, approve_label: str, reject_url: str, reject_label: str, locale: str = "en"
) -> str:
    """Approve and reject side by side: approve is the one saffron button (ink text), reject
    a danger outline (danger ink on the card, 7.4:1 light and 7.7:1 dark), never white text
    on a colour. A spacer cell keeps the gap the same in both directions. Template
    expressions such as ``{{ approve_url }}`` pass through unchanged (escaping leaves
    braces alone), so system templates can build their HTML with this."""
    sans = _sans(locale)
    reject = escape(reject_url)
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="border-collapse:separate;margin:8px 0 16px"><tr>'
        f"{_saffron_cell(str(escape(approve_url)), approve_label, sans)}"
        '<td width="12" style="width:12px;font-size:0;line-height:0">&nbsp;</td>'
        f'<td class="pl-btn-danger" align="center" style="border-radius:6px;'
        f'border:2px solid {_EMAIL_DANGER}">'
        f'<a class="pl-btn-danger-a" href="{reject}" target="_blank" rel="noopener" '
        f'style="display:inline-block;padding:10px 22px;border-radius:6px;font-family:{sans};'
        f"font-size:15px;font-weight:600;line-height:20px;color:{_EMAIL_DANGER};"
        f'text-decoration:none">{escape(reject_label)}</a></td></tr></table>'
    )


def _email_link(url: str, label: str) -> str:
    return (
        f'<a class="pl-link" href="{escape(url)}" style="color:{_EMAIL_LINK}">{escape(label)}</a>'
    )


def _split_link(line: str, links: frozenset[str]) -> tuple[str | None, str] | None:
    """``Label: URL`` or a bare ``URL`` on its own line, where the URL is the value of a
    declared ``url`` variable. Text from outsiders can never become a button this way."""
    stripped = line.strip()
    if not stripped:
        return None
    if stripped in links:
        return None, stripped
    head, _, tail = stripped.rpartition(" ")
    if tail in links and head.endswith(":") and head[:-1].strip():
        return head[:-1].strip(), tail
    return None


def _html_from_text(text: str, links: frozenset[str] = frozenset(), locale: str = "en") -> str:
    """HTML for a template without its own HTML body: paragraphs and line breaks from the
    text, every line escaped. A line holding a declared link becomes a link; the first one
    becomes the email's one button."""
    blocks: list[str] = []
    button_used = False
    for paragraph in (p for p in text.split("\n\n") if p.strip()):
        lines: list[str] = []
        for line in paragraph.splitlines():
            found = _split_link(line, links)
            if found is None:
                lines.append(str(escape(line)))
                continue
            label, url = found
            if button_used:
                lines.append(_email_link(url, label or url))
                continue
            button_used = True
            if lines:
                blocks.append(_paragraph(lines))
                lines = []
            blocks.append(email_button(url, label or _copy(locale)["open"], locale))
        if lines:
            blocks.append(_paragraph(lines))
    return "".join(blocks)


def _paragraph(lines: list[str]) -> str:
    """One paragraph of already escaped lines."""
    return "<p>" + "<br>".join(lines) + "</p>"


def _preheader(text: str | None) -> str:
    if not text:
        return ""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return " ".join(first.split())[:140]


def html_document(
    inner: str,
    locale: str,
    title: str | None,
    *,
    org_name: str | None = None,
    preheader: str | None = None,
) -> str:
    """The email HTML shell: language, direction (RTL for Arabic, every cell aligned to
    the start side and paddings symmetric so nothing needs flipping), the paper and card
    layout, the organisation header, the title, the body and the footer."""
    direction = "rtl" if locale in RTL_LOCALES else "ltr"
    align = "right" if direction == "rtl" else "left"
    copy = _copy(locale)
    sans = _sans(locale)
    org = (org_name or "").strip() or "Pairly"
    # Arabic has no glyphs in Fraunces or Georgia: the title stays in the Arabic sans,
    # set heavier, like the web app's headings. No letter spacing or capitals on Arabic.
    if direction == "rtl":
        title_font = f"font-family:{sans};font-weight:600"
        org_style = "font-size:13px;letter-spacing:0"
    else:
        title_font = f"font-family:{_SERIF};font-weight:400"
        org_style = "font-size:12px;letter-spacing:0.12em;text-transform:uppercase"
    hidden = (
        "display:none!important;visibility:hidden;mso-hide:all;font-size:1px;line-height:1px;"
        "max-height:0;max-width:0;opacity:0;overflow:hidden"
    )
    pre = _preheader(preheader)
    parts = [
        f'<!doctype html><html lang="{escape(locale)}" dir="{direction}">',
        '<head><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        '<meta name="x-apple-disable-message-reformatting">'
        f"<title>{escape(title or '')}</title>{_EMAIL_STYLE}</head>",
        f'<body dir="{direction}" class="pl-bg" style="margin:0;padding:0;'
        f'background:{_EMAIL_PAPER};text-align:{align}">',
    ]
    if pre:
        parts.append(f'<div style="{hidden}">{escape(pre)}' + "&#8203;&nbsp;" * 24 + "</div>")
    parts.append(
        f'<table role="presentation" class="pl-bg" width="100%" cellpadding="0" '
        f'cellspacing="0" border="0" dir="{direction}" bgcolor="{_EMAIL_PAPER}" '
        f'style="width:100%;background:{_EMAIL_PAPER}"><tr>'
        '<td align="center" style="padding:32px 12px">'
        '<!--[if mso]><table role="presentation" width="600" cellpadding="0" '
        'cellspacing="0" border="0"><tr><td><![endif]-->'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" dir="{direction}" style="width:100%;max-width:600px">'
        # The organisation, small, above the card.
        f'<tr><td class="pl-pad pl-muted" align="{align}" style="padding:0 32px 14px;'
        f"font-family:{sans};{org_style};line-height:18px;font-weight:600;"
        f'color:{_EMAIL_MUTED};text-align:{align}">{escape(org)}</td></tr>'
        # The card.
        f'<tr><td class="pl-card" bgcolor="{_EMAIL_CARD}" style="background:{_EMAIL_CARD};'
        f'border:1px solid {_EMAIL_RULE};border-radius:10px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'border="0" dir="{direction}">'
    )
    if title:
        parts.append(
            f'<tr><td class="pl-pad pl-ink pl-title" align="{align}" style="padding:32px 40px 4px;'
            f"{title_font};font-size:28px;line-height:34px;color:{_EMAIL_INK};"
            f'text-align:{align}">{escape(title)}</td></tr>'
        )
    parts.append(
        f'<tr><td class="pl-pad pl-body" align="{align}" style="padding:'
        f"{'20px' if title else '32px'} 40px 20px;font-family:{sans};font-size:16px;"
        f'line-height:25px;color:{_EMAIL_INK};text-align:{align}">{inner}</td></tr>'
        "</table></td></tr>"
        # The footer: why this email came, and the unsubscribe link when there is one.
        f'<tr><td class="pl-pad pl-muted" align="{align}" style="padding:18px 32px 0;'
        f"font-family:{sans};font-size:12px;line-height:18px;color:{_EMAIL_MUTED};"
        f'text-align:{align}">'
        f'<p style="margin:0 0 6px">{escape(copy["why"].format(org=org))}</p>'
        f"{UNSUBSCRIBE_SLOT}</td></tr>"
        "</table>"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></body></html>"
    )
    return "".join(parts)


def fill_unsubscribe(html: str | None, url: str | None, locale: str) -> str | None:
    """Put the signed one-click unsubscribe link in the footer slot (the last one, so a
    copy of the marker inside a template body is never the one filled)."""
    if not html or not url:
        return html
    head, sep, tail = html.rpartition(UNSUBSCRIBE_SLOT)
    if not sep:
        return html
    link = (
        f'<p style="margin:0"><a class="pl-muted" href="{escape(url)}" '
        f'style="color:{_EMAIL_MUTED};text-decoration:underline">'
        f"{escape(_copy(locale)['unsubscribe'])}</a></p>"
    )
    return head + link + tail


def render_source(
    src: TemplateSource,
    values: Mapping[str, Any],
    *,
    tz: str | None = None,
    org_name: str | None = None,
) -> Rendered:
    """Render every part of a template version for one recipient.

    The whole version is linted again first: a stored row can never skip the checks.
    ``org_name`` heads the email; the result is a pure function of its inputs, so a
    sensitive message renders to the same bytes when it is approved and when it is sent.
    """
    errors = [i for i in lint_source(src) if i.severity == "error"]
    if errors:
        raise TemplateRenderError(errors[0].message)
    clean = coerce_variables(src.variables, values)
    subject = None
    if src.subject:
        subject = _single_line(render_part(src.subject, "text", clean, locale=src.locale, tz=tz))
    body_fmt: Format = "slack" if src.channel == "slack" else "text"
    text = render_part(src.body, body_fmt, clean, locale=src.locale, tz=tz).strip()
    html = None
    if src.channel == "email":
        links = frozenset(
            value
            for name, spec in src.variables.items()
            if spec.type == "url" and isinstance(value := clean.get(name), str) and value
        )
        inner = (
            render_part(src.body_html, "html", clean, locale=src.locale, tz=tz)
            if src.body_html
            else _html_from_text(text, links, src.locale)
        )
        html = html_document(inner, src.locale, subject, org_name=org_name, preheader=text)
    return Rendered(subject=subject, text=text, html=html, locale=src.locale)
