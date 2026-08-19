import hashlib
import json
import re
import unicodedata
from html.parser import HTMLParser
from typing import Any, Iterable, List, Sequence, Tuple


_HASHTAG_RE = re.compile(r"(?<!#)#([\w\u4e00-\u9fff-]{1,30})", re.UNICODE)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_IMAGE_URL_RE = re.compile(r"https?://[^\s<>\"]+\.(?:png|jpe?g|gif|webp)(?:\?[^\s<>\"]*)?", re.I)
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")
_SPACE_RE = re.compile(r"\s+")


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.media_urls: List[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, str]]) -> None:
        if tag.casefold() not in {"img", "video", "source"}:
            return
        for key, value in attrs:
            if key.casefold() == "src" and value:
                self.media_urls.append(value)


def extract_plain_text(value: str) -> str:
    if not value:
        return ""
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        else:
            parts: List[str] = []
            _collect_json_text(parsed, parts)
            if parts:
                return "\n".join(parts)

    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
    except Exception:
        return value
    return " ".join(parser.parts) if parser.parts else value


def extract_media_urls(value: str) -> Tuple[str, ...]:
    if not value:
        return tuple()
    found: List[str] = []
    stripped = value.strip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        else:
            _collect_json_media(parsed, found)

    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        found.extend(parser.media_urls)
    except Exception:
        pass
    found.extend(_MARKDOWN_IMAGE_RE.findall(value))
    found.extend(_IMAGE_URL_RE.findall(value))
    return tuple(_deduplicate_preserving_order(found))


def extract_hashtags(text: str) -> Tuple[str, ...]:
    normalized = []
    for match in _HASHTAG_RE.finditer(text or ""):
        tag = unicodedata.normalize("NFKC", match.group(1)).strip().casefold()
        if tag and tag not in normalized:
            normalized.append(tag)
    return tuple(normalized)


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    normalized = _ZERO_WIDTH_RE.sub("", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def content_fingerprint(title: str, body: str, media_urls: Iterable[str]) -> str:
    plain_title = normalize_text(extract_plain_text(title))
    plain_body = normalize_text(extract_plain_text(body))
    normalized_media = [normalize_text(url) for url in media_urls if normalize_text(url)]
    canonical = json.dumps(
        {"title": plain_title, "body": plain_body, "media": normalized_media},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _collect_json_text(value: Any, parts: List[str], key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            _collect_json_text(child, parts, str(child_key).casefold())
    elif isinstance(value, list):
        for child in value:
            _collect_json_text(child, parts, key)
    elif isinstance(value, str) and key in {"text", "desc", "title", "content"} and value.strip():
        parts.append(value)


def _collect_json_media(value: Any, found: List[str], key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            _collect_json_media(child, found, str(child_key).casefold())
    elif isinstance(value, list):
        for child in value:
            _collect_json_media(child, found, key)
    elif isinstance(value, str) and key in {"url", "src"} and value.startswith(("http://", "https://")):
        found.append(value)


def _deduplicate_preserving_order(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
