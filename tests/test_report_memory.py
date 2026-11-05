"""Memoria analitica: tema/edicao, autorizacao, versoes e auditoria."""
from datetime import date
from io import BytesIO
from types import SimpleNamespace

from pypdf import PdfReader
from reportlab.platypus import SimpleDocTemplate
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import AppUser, Project, ReportVersion
from app.reports.pdf.history import add_historical_comparison_section
from app.reports.pdf.layout import pdf_styles
from app.services.report_memory import previous_analyses, same_topic


def _project(topic, project_type="INSTITUTIONAL_PRODUCT"):
    return SimpleNamespace(topic=topic, project_type=project_type)


def test_dossie_edition_is_not_confused():
    assert same_topic(
        _project("Dossiê Mulher 2026"),
        _project("Repercussão midiática do Dossiê Mulher 2026"),
    )
    assert not same_topic(_project("Dossiê Mulher 2026"), _project("Dossiê Mulher 2025"))
    assert not same_topic(
        _project("Dossiê Mulher 2026"),
        _project("Violência contra mulheres no RJ", "GENERAL_TOPIC"),
    )


def _new_project(db, topic, owner):
    p = Project(
        topic=topic,
        owner_id=owner,
        project_type="INSTITUTIONAL_PRODUCT",
        collection_start=date(2026, 7, 1),
        collection_end=date(2026, 7, 31),
        launch_date=date(2026, 7, 1),
        has_custom_date_window=True,
    )
    db.add(p)
    db.flush()
    return p


def _report(db, project, *, status="APPROVED", urls=None):
    payload = {
        "report": {
            "executive_summary": "Visão da análise anterior sobre o tema.",
            "dominant_framing": "Discussão sobre a violência psicológica.",
            "thematic_axes": [{"axis": "Violência psicológica", "coverage": "Mídia anterior."}],
        },
        "metrics": {"valid_items": len(urls or []), "unique_vehicles": 1},
        "corpus": [
            {"url": u, "title": "Reportagem anterior"}
            for u in (urls or [])
        ],
    }
    v = ReportVersion(
        project_id=project.id,
        version_no=1,
        content_hash="digest-" + str(project.id),
        body=payload,
        qa_status=status,
    )
    db.add(v)
    db.flush()
    return v


def test_previous_readings_are_private_edition_bound_and_approved():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add_all([
                AppUser(id="owner-a", email="owner-a@example.org", plan="FREE"),
                AppUser(id="owner-b", email="owner-b@example.org", plan="FREE"),
            ])
            db.flush()
            current = _new_project(db, "Dossiê Mulher 2026", "owner-a")
            prior = _new_project(db, "Repercussão do Dossiê Mulher 2026", "owner-a")
            other_edition = _new_project(db, "Dossiê Mulher 2025", "owner-a")
            other_owner = _new_project(db, "Dossiê Mulher 2026", "owner-b")
            rejected = _new_project(db, "Dossiê Mulher 2026", "owner-a")
            old_version = _report(db, prior, urls=[
                "https://example.org/mesma?utm_source=teste",
                "https://example.org/antiga",
            ])
            _report(db, other_edition, urls=["https://example.org/2025"])
            _report(db, other_owner, urls=["https://example.org/segredo"])
            _report(db, rejected, status="REJECTED", urls=["https://example.org/rejeitado"])
            db.commit()

            hist = previous_analyses(
                db,
                current,
                [
                    {"url": "https://www.example.org/mesma", "title": "Mesmo documento"},
                    {"url": "https://example.org/novo", "title": "Novo na amostra"},
                ],
            )
            assert hist["status"] == "AVAILABLE"
            assert len(hist["prior_reports"]) == 1
            assert hist["prior_reports"][0]["report_version_id"] == old_version.id
            basis = hist["comparison_basis"]
            assert basis["same_explicit_window"] is True
            assert basis["shared_document_count"] == 1
            assert basis["new_to_current_sample_count"] == 1
            assert basis["absent_from_current_sample_count"] == 1

            # Janela diferente invalida comparabilidade, mesmo tema/edicao.
            current.collection_end = date(2026, 8, 1)
            hist2 = previous_analyses(db, current, [])
            assert hist2["comparison_basis"]["same_explicit_window"] is False
    finally:
        engine.dispose()


def test_legacy_project_without_owner_only_compares_own_versions():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            current = _new_project(db, "Dossiê Mulher 2026", None)
            other = _new_project(db, "Dossiê Mulher 2026", None)
            _report(db, other)
            db.commit()
            assert previous_analyses(db, current, [])["status"] == "NO_PRIOR_APPROVED_REPORT"
            _report(db, current)
            db.commit()
            result = previous_analyses(db, current, [])
            assert len(result["prior_reports"]) == 1
            assert result["prior_reports"][0]["project_id"] == current.id
    finally:
        engine.dispose()


def test_pdf_comparison_displays_provenance_and_limitations():
    history = {
        "status": "AVAILABLE",
        "prior_reports": [{
            "project_id": 12,
            "version_no": 2,
            "report_version_id": 34,
            "created_at": "2026-07-12T10:00:00",
        }],
        "comparison_basis": {
            "previous_corpus_items_with_url": 3,
            "current_corpus_items_with_url": 4,
            "shared_document_count": 1,
            "new_to_current_sample_count": 3,
            "absent_from_current_sample_count": 2,
            "interpretation": "Janelas diferentes, não interpretar como tendência.",
        },
        "interpretation": {
            "summary": "A amostra atual destaca outra dimensão editorial.",
            "continuities": ["Violência psicológica permanece em discussão."],
            "changes": ["Outra ênfase narrativa na amostra atual."],
            "caveats": ["Não é possível estimar crescimento de cobertura."],
        },
    }
    styles = pdf_styles()
    story = []
    assert add_historical_comparison_section(
        story, history,
        heading=styles["heading"], body=styles["body"], small=styles["small"],
    )
    out = BytesIO()
    SimpleDocTemplate(out).build(story)
    text = "".join(page.extract_text() or "" for page in PdfReader(BytesIO(out.getvalue())).pages)
    assert "Projeto 12" in text
    assert "versão 2" in text
    assert "Janelas diferentes" in text
    assert "não são necessariamente" in text
