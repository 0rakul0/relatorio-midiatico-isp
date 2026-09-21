from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import CorpusDocument, MediaItem, Project
from app.services.corpus_metadata import infer_publication_date, repair_corpus_metadata


def _session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_infer_publication_date_from_url_and_title():
    assert infer_publication_date(
        "https://g1.globo.com/rj/noticia/2026/05/26/exemplo.ghtml"
    ) == date(2026, 5, 26)
    assert infer_publication_date(
        "Combater o crime sem ferir a Constituição - 03/09/2026 - Folha"
    ) == date(2026, 9, 3)
    assert infer_publication_date("https://site.com/noticias/2026/09/sem-dia") is None


def test_repair_corpus_metadata_fixes_origin_and_propagates_date():
    session = _session()
    project = Project(
        topic="tema",
        owner_id="u1",
        launch_date=date(2026, 1, 1),
        collection_start=date(2026, 1, 1),
        collection_end=date(2026, 12, 31),
    )
    session.add(project)
    session.flush()

    document = CorpusDocument(
        canonical_url="https://www.youtube.com/watch?v=abc",
        url="https://www.youtube.com/watch?v=abc",
        title="Video 03/09/2026",
        media_origin="PORTAL_NOTICIAS",
    )
    session.add(document)
    session.flush()

    item = MediaItem(
        project_id=project.id,
        corpus_document_id=document.id,
        title="Video 03/09/2026",
        url=document.url,
        canonical_url=document.canonical_url,
        media_origin="PORTAL_NOTICIAS",
        status="VALID",
    )
    session.add(item)
    session.commit()

    stats = repair_corpus_metadata(session)
    session.commit()

    assert document.media_origin == "YOUTUBE"
    assert item.media_origin == "YOUTUBE"
    assert document.published_at == date(2026, 9, 3)
    assert item.published_at == date(2026, 9, 3)
    assert stats["origins_fixed"] >= 2
    assert stats["dates_inferred"] >= 2
    session.close()
