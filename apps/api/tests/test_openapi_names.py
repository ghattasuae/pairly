# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""OpenAPI component names are unique across core and every plugin.

When two different Pydantic models share a class name, FastAPI keeps both but names
their components after the module (``pairly_pay__api__exceptions__TemplateOut``). The
generated TypeScript then changes names whenever another plugin adds a model, and the
web app's ``S["TemplateOut"]`` silently points somewhere else. Plugins prefix their
models (``Pay...``, ``Hire...``) so this never happens; this test keeps it that way.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, create_model

from pairly_api.main import create_app
from pairly_core.plugins import discover_plugins

from .conftest import make_settings


def clashing_components(schema: dict[str, Any]) -> list[str]:
    """Component names FastAPI had to qualify with a module path (a name clash)."""
    return sorted(k for k in schema.get("components", {}).get("schemas", {}) if "__" in k)


def test_no_two_models_share_a_component_name() -> None:
    plugins = list(discover_plugins().values())
    assert {p.id for p in plugins} >= {"hire", "pay"}
    schema = create_app(make_settings(), plugins=plugins).openapi()
    assert clashing_components(schema) == []


def _model(module: str, field: str) -> type[BaseModel]:
    model: type[BaseModel] = create_model("DraftOut", __module__=module, **{field: (str, ...)})
    return model


def test_the_check_catches_a_clash_between_two_plugins() -> None:
    app = FastAPI()
    for plugin, field in (("one", "text"), ("two", "body")):
        router = APIRouter()
        model = _model(f"plugin_{plugin}.api", field)
        router.add_api_route(f"/{plugin}", lambda: None, response_model=model)
        app.include_router(router)
    assert clashing_components(app.openapi()) == [
        "plugin_one__api__DraftOut",
        "plugin_two__api__DraftOut",
    ]
