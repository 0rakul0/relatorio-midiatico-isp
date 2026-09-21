"""Guards arquiteturais: separação SERVICES → REPORTA GENT → TOOLS → PROVIDERS.

Apenas ``app/tools/*`` pode tocar nos provedores de pesquisa, e apenas
``app/agent.py`` pode executar ``tool.invoke(...)``. Serviços informam a
metodologia e recebem sinks/observers; nunca falam com provedores.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"


def _py_files() -> list[Path]:
    return [path for path in APP_ROOT.rglob("*.py") if path.name != "__init__"]


def test_providers_only_imported_inside_tools():
    offenders = []
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        if not re.search(
            r"from\s+app\.tools\.providers|import\s+app\.tools\.providers|from\s+\.\.providers\s+import",
            text,
        ):
            continue
        if not str(path.relative_to(APP_ROOT)).startswith("tools"):
            offenders.append(str(path.relative_to(APP_ROOT.parent)))
    assert offenders == [], f"módulos fora de app/tools importando providers: {offenders}"


def test_tool_invoke_only_in_agent():
    offenders = []
    for path in _py_files():
        if str(path.relative_to(APP_ROOT)) == "agent.py":
            continue
        in_docstring = False
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.startswith("'''"):
                in_docstring = not in_docstring
                continue
            if in_docstring or stripped.startswith("#"):
                continue
            if re.search(r"\b\w+\.invoke\(", line):
                offenders.append(f"{path.relative_to(APP_ROOT.parent)}:{lineno}")
    assert offenders == [], f"tool.invoke() fora de app/agent.py: {offenders}"


def test_services_do_not_import_provider_tools():
    offenders = []
    for path in _py_files():
        if not str(path.relative_to(APP_ROOT)).startswith("services"):
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"from\s+app\.tools\.(search|hydration)\s+import", text) and not re.search(
            r"search_providers_available|build_agent_tools|SearchObserver",
            text,
        ):
            offenders.append(str(path.relative_to(APP_ROOT.parent)))
    assert offenders == []