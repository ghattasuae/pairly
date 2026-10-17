# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Proofly-Commercial
# Copyright (c) 2026 Proofly Technology
"""``docs/graphs`` is generated from the graph code: regenerating it changes nothing."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "export_graphs", ROOT / "scripts" / "export_graphs.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_graphs"] = module
    spec.loader.exec_module(module)
    return module


export_graphs = _load()


def test_committed_graph_diagrams_match_the_code() -> None:
    assert export_graphs.stale() == [], "run: uv run python -m scripts.export_graphs"


def test_every_registered_graph_has_a_diagram() -> None:
    files = export_graphs.render()
    index = files["README.md"]
    for plugin_id, _, spec in export_graphs._specs():
        assert f"{spec.name}.md" in files
        assert f"| {plugin_id} | `{spec.name}` |" in index
        assert "```mermaid" in files[f"{spec.name}.md"]


def test_check_mode_reports_a_stale_file(tmp_path: Path) -> None:
    page = tmp_path / "architecture.md"
    page.write_text(
        f"# Page\n\n{export_graphs.BEGIN}\nold\n{export_graphs.END}\nafter\n", encoding="utf-8"
    )
    out = tmp_path / "graphs"
    assert "architecture.md" in export_graphs.stale(out, page)
    export_graphs.write(out, page)
    assert export_graphs.stale(out, page) == []
    text = page.read_text(encoding="utf-8")
    assert text.startswith("# Page\n\n")
    assert text.endswith("\nafter\n")
    assert "### `pay_invoice` (Pairly Pay)" in text
    (out / "pay_invoice.md").write_text("edited by hand\n", encoding="utf-8")
    (out / "old_graph.md").write_text("gone\n", encoding="utf-8")
    assert export_graphs.stale(out, page) == ["old_graph.md", "pay_invoice.md"]
