# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 Proofly Technology
"""Engine services a graph node may use (``runtime.context.services``).

Graph nodes need the same engine services the API and the worker hold: the document
store (to read a parsed document), the model router (extraction, checks), and, for
graphs that drive a document through intake themselves, the virus scanner and the parse
settings. The process that runs graphs (API or worker) builds one ``GraphServices`` and
hands it to ``WorkflowRuntime``; every run's ``WorkflowContext`` carries it.

Every field may be ``None``: a process may not have a service (the API has no scanner
when ClamAV is not configured, a test passes only what it needs). A node that needs a
missing service must fail closed (send the item to a person with the reason), never
skip the step silently.

Workflow agnostic: nothing here knows about a workflow. A workflow that needs another
kind of service builds it from its own settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pairly_core.documents.pipeline import ParseConfig
    from pairly_core.ingestion.scan import VirusScanner
    from pairly_core.ingestion.storage import ObjectStore
    from pairly_core.llm.router import ModelRouter


@dataclass(frozen=True, slots=True)
class GraphServices:
    store: ObjectStore | None = None
    router: ModelRouter | None = None
    scanner: VirusScanner | None = None
    parse_config: ParseConfig | None = None


NO_SERVICES = GraphServices()
