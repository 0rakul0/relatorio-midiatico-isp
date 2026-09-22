from sqlalchemy import Text

from app.models import Classification


def test_classification_framing_is_unbounded_text():
    assert isinstance(Classification.__table__.c.framing.type, Text)
