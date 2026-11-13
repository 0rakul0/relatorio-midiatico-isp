"""Auditable, offline snapshots of completed media research runs."""
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.models import (
    Project, SearchQuery, SearchCall, SearchHit, MediaItem,
    Classification, ReportRun,
)

_RUN_RE = re.compile(r"^[0-9a-f]{32}$")
ROOT = Path(os.environ.get("ISP_REPRO_DIR", "data/reproducibility")).resolve()


def _folder(run_id):
    if not _RUN_RE.fullmatch(run_id):
        raise ValueError("Invalid run identifier")
    return ROOT / run_id


def _encode(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _bytes(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), default=_encode) + "\n").encode("utf-8")


def _table_rows(db, model, condition):
    rows = db.scalars(select(model).where(condition).order_by(model.id)).all()
    return [{column.key: getattr(row, column.key)
             for column in model.__table__.columns} for row in rows]


def _write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False) as tmp:
        tmp.write(content)
        name = tmp.name
    os.replace(name, path)


def snapshot(db, project_id, run_id):
    """Capture persisted evidence; never calls search providers or LLMs."""
    folder = _folder(run_id)
    project = db.get(Project, project_id)
    if project is None:
        raise ValueError("Project not found")
    run = db.scalar(select(ReportRun).where(ReportRun.run_id == run_id))
    if run is None or run.project_id != project_id:
        raise ValueError("Run does not belong to project")
    data = {
        "project": {c.key: getattr(project, c.key) for c in Project.__table__.columns},
        "run": {c.key: getattr(run, c.key) for c in ReportRun.__table__.columns},
        "queries": _table_rows(db, SearchQuery, SearchQuery.project_id == project_id),
        "search_calls": _table_rows(db, SearchCall, SearchCall.run_id == run_id),
        "search_hits": _table_rows(db, SearchHit, SearchHit.run_id == run_id),
        "media_items": _table_rows(db, MediaItem, MediaItem.project_id == project_id),
        "classifications": _table_rows(
            db, Classification,
            Classification.media_item_id.in_(
                select(MediaItem.id).where(MediaItem.project_id == project_id)
            ),
        ),
    }
    content = _bytes(data)
    digest = hashlib.sha256(content).hexdigest()
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sha256": digest,
        "counts": {k: len(v) for k, v in data.items() if isinstance(v, list)},
        "scope": "Persisted project state and run search evidence; not a replay of external providers or LLMs",
    }
    _write_atomic(folder / "snapshot.json", content)
    _write_atomic(folder / "manifest.json", _bytes(manifest))
    return manifest


def verify(run_id):
    folder = _folder(run_id)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("run_id") != run_id:
        raise ValueError("Manifest run mismatch")
    raw = (folder / "snapshot.json").read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    return {"ok": actual == manifest["sha256"], "expected": manifest["sha256"],
            "actual": actual, "counts": manifest.get("counts", {})}


def replay(run_id):
    """Return immutable saved evidence, without internet or LLM access."""
    if not verify(run_id)["ok"]:
        raise ValueError("Snapshot checksum mismatch")
    return json.loads((_folder(run_id) / "snapshot.json").read_text(encoding="utf-8"))
