from __future__ import annotations

import re
import unicodedata


STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "por", "com", "um", "uma", "ao",
    "aos", "que", "sobre", "qual", "quais", "como", "foi", "foram", "ser",
    "tem", "teve", "mais", "menos", "entre", "me", "diga", "mostre",
}

CASUAL_GREETINGS = {
    "oi", "ola", "opa", "e ai", "bom dia", "boa tarde", "boa noite",
    "oi tudo bem", "ola tudo bem", "e ai tudo bem", "tudo bem",
}
CASUAL_THANKS = {
    "obrigado", "obrigada", "muito obrigado", "muito obrigada", "valeu",
    "agradecido", "agradecida",
}
CASUAL_FAREWELLS = {
    "tchau", "ate mais", "ate logo", "falou",
}


def topic_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(
        char for char in text if not unicodedata.combining(char)
    )
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def tokens(value: str | None) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", topic_key(value))
        if len(token) >= 3 and token not in STOPWORDS
    }


def latest_user_question(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if str(message.get("role") or "").lower() == "user":
            return str(message.get("content") or "").strip()
    return ""


def casual_kind(question: str) -> str | None:
    normalized = topic_key(question)
    if normalized in CASUAL_GREETINGS:
        return "GREETING"
    if normalized in CASUAL_THANKS:
        return "THANKS"
    if normalized in CASUAL_FAREWELLS:
        return "FAREWELL"
    return None


def retrieval_question(messages: list[dict]) -> str:
    user_messages = [
        str(message.get("content") or "").strip()
        for message in (messages or [])
        if str(message.get("role") or "").lower() == "user"
        and str(message.get("content") or "").strip()
    ]
    if not user_messages:
        return ""

    latest = user_messages[-1]
    if len(tokens(latest)) >= 2:
        return latest

    for previous in reversed(user_messages[:-1]):
        if tokens(previous):
            return f"{previous}\n{latest}"
    return latest


def normalize_member_references(answer: str, by_index: dict[int, dict]) -> str:
    text = str(answer or "")

    def replace(match: re.Match) -> str:
        index = int(match.group(1))
        if index not in by_index:
            return match.group(0)
        return f"[F{index + 1}]"

    return re.sub(
        r"[\(\[]?\s*Index\s+(\d+)\s*[\)\]]?",
        replace,
        text,
        flags=re.IGNORECASE,
    )
