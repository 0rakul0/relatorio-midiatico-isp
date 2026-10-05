from __future__ import annotations

import re
from typing import Any


VIEW_FIELD_PATHS = (
    "viewCount",
    "viewsCount",
    "views",
    "view_count",
    "videoViewCount",
    "videoViews",
    "playCount",
    "play_count",
    "plays",
    "statistics.viewCount",
    "statistics.views",
    "statistics.playCount",
    "metrics.viewCount",
    "metrics.views",
    "metrics.playCount",
    "post.viewCount",
    "post.viewsCount",
    "post.views",
    "post.videoViewCount",
    "post.playCount",
    "metadata.viewCount",
    "metadata.views",
    "metadata.playCount",
    "metadata.sourceTweet.viewCount",
    "metadata.sourceTweet.views",
    "sourceTweet.viewCount",
    "sourceTweet.views",
    "tweet.viewCount",
    "tweet.views",
)


def _nested(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def int_metric(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = int(value)
        return parsed if parsed >= 0 else None

    text = str(value).strip().lower().replace(" ", " ")
    if not text:
        return None

    compact = text.replace(" ", "")
    suffix_match = re.fullmatch(
        r"([0-9]+(?:[\.,][0-9]+)?)([kmb])",
        compact,
    )
    if suffix_match:
        number = float(suffix_match.group(1).replace(",", "."))
        multiplier = {
            "k": 1_000,
            "m": 1_000_000,
            "b": 1_000_000_000,
        }[suffix_match.group(2)]
        return max(0, int(round(number * multiplier)))

    digits = re.sub(r"[^0-9]", "", text)
    if digits:
        return int(digits)
    return None


def post_view_count(row: dict[str, Any] | None) -> tuple[int | None, str | None]:
    if not isinstance(row, dict):
        return None, None
    for path in VIEW_FIELD_PATHS:
        parsed = int_metric(_nested(row, path))
        if parsed is not None:
            return parsed, path
    return None, None
