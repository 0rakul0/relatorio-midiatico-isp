"""Hashes estáveis para versionamento e cache de relatórios.

Separado de ``reporting.py`` e ``cache.py`` para evitar import circular:
``reporting`` importa ``hydrate_cached_report`` de ``cache``, que precisa do
fingerprint do projeto na heurística de validade do snapshot.
"""

from __future__ import annotations

import hashlib
import json

from app.models import Project


def request_fingerprint(project: Project) -> str:
    """Identifica o estado de entrada do relatório para invalidar cache/servir versão."""
    raw = json.dumps(
        {
            "topic": project.topic,
            "collection_start": project.collection_start.isoformat() if project.collection_start else None,
            "collection_end": project.collection_end.isoformat() if project.collection_end else None,
            "project_type": project.project_type,
            "execution_profile": project.execution_profile,
            "execution_options": project.execution_options or {},
            "topic_profile": project.topic_profile or {},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def content_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()