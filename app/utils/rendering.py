from __future__ import annotations


def text(value: object) -> str:
    return (
        str(value or "")
        .replace("—", "-")
        .replace("–", "-")
        .replace("“", '"')
        .replace("”", '"')
        .replace("’", "'")
    )


def has_content(value: object) -> bool:
    """True somente quando há conteúdo editorial realmente renderizável."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, dict):
        return any(has_content(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(has_content(item) for item in value)
    return True


def has_meaningful_fields(item: object, fields: tuple[str, ...]) -> bool:
    if not isinstance(item, dict):
        return False
    return any(has_content(item.get(field)) for field in fields)
