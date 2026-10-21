"""Safe content decoding, quality checks, and text extraction for WebFetch."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from bs4 import BeautifulSoup, NavigableString, Tag

from .models import FetchError


_TEXTUAL_TYPES = frozenset({
    "text/html",
    "application/xhtml+xml",
    "text/markdown",
    "text/plain",
    "application/json",
    "application/xml",
    "text/xml",
    "application/javascript",
    "text/javascript",
})
_REMOVED_HTML_TAGS = (
    "script", "style", "noscript", "iframe", "template", "svg", "canvas",
)
_BOILERPLATE_TAGS = frozenset({
    "nav", "footer", "aside", "menu", "dialog", "form",
})
_BOILERPLATE_ROLES = frozenset({
    "navigation", "banner", "contentinfo", "complementary", "search",
})
_BOILERPLATE_HINT = re.compile(
    r"(?:^|[-_])(?:nav|navbar|navigation|menu|sidebar|footer|breadcrumbs?|"
    r"cookie|consent|advert|ads?|promo|toolbar)(?:[-_]|$)",
    re.I,
)
_LINK_MARKDOWN = re.compile(r"\[([^\]]*)\]\([^)]*\)")


@dataclass(frozen=True)
class ExtractedContent:
    text: str | None
    content_type: str
    binary: bool = False
    offset: int = 0
    text_length: int = 0
    link_text_length: int = 0
    paragraph_count: int = 0
    empty_content: bool = False
    navigation_only: bool = False


@dataclass(frozen=True)
class _Metrics:
    text_length: int = 0
    link_text_length: int = 0
    paragraph_count: int = 0
    heading_count: int = 0
    pre_count: int = 0

    @property
    def link_density(self) -> float:
        if self.text_length <= 0:
            return 0.0
        return self.link_text_length / self.text_length


def _media_type(content_type: str) -> str:
    return str(content_type or "").split(";", 1)[0].strip().lower() or "application/octet-stream"


def _charset(content_type: str) -> str:
    match = re.search(r"charset\s*=\s*[\"']?([^;\"'\s]+)", str(content_type or ""), re.I)
    return match.group(1) if match else "utf-8"


def decode_text(body: bytes, content_type: str) -> str:
    encoding = _charset(content_type)
    try:
        return body.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise FetchError(
            "DECODE_FAILED",
            f"Could not decode response as {encoding}",
            details={"charset": encoding},
        ) from exc


def _tag_hint(node: Tag) -> str:
    values = []
    node_id = node.get("id")
    if node_id:
        values.append(str(node_id))
    classes = node.get("class") or ()
    if isinstance(classes, str):
        values.append(classes)
    else:
        values.extend(str(item) for item in classes)
    return " ".join(values)


def _is_structural_boilerplate(node: Tag) -> bool:
    name = str(node.name or "").lower()
    if name in _BOILERPLATE_TAGS:
        return True
    role = str(node.get("role") or "").strip().lower()
    if role in _BOILERPLATE_ROLES:
        return True
    if node.has_attr("hidden"):
        return True
    if str(node.get("aria-hidden") or "").strip().lower() == "true":
        return True
    hint = _tag_hint(node)
    if hint and _BOILERPLATE_HINT.search(hint):
        return not node.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    return False


def _is_global_header(node: Tag) -> bool:
    if str(node.name or "").lower() != "header":
        return False
    if node.find_parent(["article", "main"]) is not None:
        return False
    # A header carrying the document's only top-level title is content, not chrome.
    return node.find(["h1", "h2", "h3", "h4", "h5", "h6"]) is None


def _ignored_for_metrics(node: Tag) -> bool:
    return _is_structural_boilerplate(node) or _is_global_header(node)


def _metrics(node: Tag, *, include_boilerplate: bool = False) -> _Metrics:
    text_length = 0
    link_text_length = 0
    paragraphs = 0
    headings = 0
    pre_blocks = 0
    for item in node.descendants:
        if isinstance(item, Tag):
            if item.name == "p":
                paragraphs += 1
            elif item.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                headings += 1
            elif item.name == "pre":
                pre_blocks += 1
            continue
        if not isinstance(item, NavigableString):
            continue
        parent = item.parent
        if not isinstance(parent, Tag):
            continue
        ignored = False
        current = parent
        stop = node if isinstance(node, Tag) else None
        while isinstance(current, Tag):
            if not include_boilerplate and _ignored_for_metrics(current):
                ignored = True
                break
            if current is stop:
                break
            current = current.parent
        if ignored:
            continue
        value = re.sub(r"\s+", " ", str(item)).strip()
        if not value:
            continue
        text_length += len(value)
        if parent.name == "a" or parent.find_parent("a") is not None:
            link_text_length += len(value)
    return _Metrics(text_length, link_text_length, paragraphs, headings, pre_blocks)


def _clean_fragment(fragment: BeautifulSoup) -> None:
    for node in list(fragment.find_all(True)):
        if node.attrs is None:
            continue
        if _is_structural_boilerplate(node) or _is_global_header(node):
            node.decompose()
            continue
        name = str(node.name or "").lower()
        if name not in {"ul", "ol", "div", "section", "table"}:
            continue
        if node.find(["p", "pre", "h1", "h2", "h3", "h4", "h5", "h6"]):
            continue
        node_metrics = _metrics(node)
        if node_metrics.text_length < 40:
            continue
        if node_metrics.link_density >= 0.8:
            node.decompose()


def _inline_text(node: Tag) -> str:
    pieces = []
    for child in node.children:
        if isinstance(child, NavigableString):
            pieces.append(str(child))
        elif isinstance(child, Tag):
            inner = _inline_text(child)
            if child.name in {"strong", "b"} and inner:
                pieces.append(f"**{inner}**")
            elif child.name in {"em", "i"} and inner:
                pieces.append(f"*{inner}*")
            elif child.name == "a" and inner:
                href = str(child.get("href") or "").strip()
                pieces.append(f"[{inner}]({href})" if href else inner)
            else:
                pieces.append(inner)
    return re.sub(r"\s+", " ", "".join(pieces)).strip()


def _fragment_to_markdown(fragment: BeautifulSoup) -> str:
    lines = []
    for node in fragment.find_all([
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote",
        "pre", "code", "br", "hr",
    ]):
        if node.find_parent(["pre", "code"]) and node.name != "code":
            continue
        name = node.name
        if name == "br":
            lines.append("")
        elif name == "hr":
            lines.append("---")
        elif name == "li":
            text = _inline_text(node)
            if text:
                lines.append(f"- {text}")
        elif name == "blockquote":
            text = _inline_text(node)
            if text:
                lines.append(f"> {text}")
        elif name == "pre":
            lines.extend(["```", node.get_text().strip("\n"), "```"])
        elif len(name) == 2 and name[0] == "h" and name[1].isdigit():
            text = _inline_text(node)
            if text:
                lines.extend([f"{'#' * int(name[1])} {text}", ""])
        else:
            text = _inline_text(node)
            if text:
                lines.extend([text, ""])
    markdown = "\n".join(lines).strip()
    if markdown:
        return markdown
    return re.sub(r"\n{3,}", "\n\n", fragment.get_text("\n", strip=True)).strip()


def _link_text(markdown: str) -> int:
    return sum(len(match.group(1).strip()) for match in _LINK_MARKDOWN.finditer(markdown))


_LEGAL_OR_COPYRIGHT = re.compile(
    r"(?:copyright|©|许可证|备案|营业执照|privacy|terms|cookie|sitemap|about)",
    re.I,
)


def _has_repeated_link_block(node: Tag) -> bool:
    for parent in node.find_all(True):
        items = [
            item for item in parent.find_all(["p", "li"], recursive=False)
            if _inline_text(item)
        ]
        if len(items) < 2:
            continue
        texts = [_inline_text(item) for item in items]
        linked = sum(_link_text(text) for text in texts)
        legal_hits = sum(bool(_LEGAL_OR_COPYRIGHT.search(text)) for text in texts)
        prose_parts = [
            re.sub(r"\s+", "", _LINK_MARKDOWN.sub(" ", text))
            for text in texts
        ]
        has_long_prose = any(len(item) >= 180 for item in prose_parts)
        total = sum(len(re.sub(r"\s+", "", text)) for text in texts)
        if (
            not has_long_prose
            and total >= 24
            and (
                linked / max(total, 1) >= 0.5
                or legal_hits >= 2
                or (legal_hits >= 1 and sum(len(item) for item in prose_parts) < 80)
            )
        ):
            return True
    return False


def _select_content_fragment(soup: BeautifulSoup) -> BeautifulSoup:
    for node in soup(_REMOVED_HTML_TAGS):
        node.decompose()

    candidates = []
    seen = set()
    for node in soup.find_all(["main", "article", "[role=main]"]):
        key = id(node)
        if key in seen:
            continue
        seen.add(key)
        score = 0
        current = node
        while isinstance(current, Tag):
            name = str(current.name or "").lower()
            role = str(current.get("role") or "").strip().lower()
            if name in {"main", "article"} or role == "main":
                score += 6
            if _is_structural_boilerplate(current) or _is_global_header(current):
                score -= 8
            current = current.parent
        item_metrics = _metrics(node)
        score += min(item_metrics.text_length // 80, 30)
        score += min(item_metrics.paragraph_count * 3, 24)
        score += min(item_metrics.heading_count * 2, 10)
        score += min(item_metrics.pre_count * 2, 8)
        score -= int(item_metrics.link_density * 20)
        if _has_repeated_link_block(node):
            score -= 12
        candidates.append((score, node))
    if candidates:
        node = max(candidates, key=lambda item: item[0])[1]
        fragment = BeautifulSoup(str(node), "html.parser")
        _clean_fragment(fragment)
        return fragment

    """Choose a content-dense subtree without domain-specific rules."""
    candidates = []
    for node in soup.find_all(["div", "section", "td", "body"]):
        metrics = _metrics(node)
        if metrics.text_length < 40:
            continue
        score = (
            min(metrics.text_length // 80, 30)
            + min(metrics.paragraph_count * 3, 24)
            + min(metrics.heading_count * 2, 10)
            + min(metrics.pre_count * 2, 8)
            - int(metrics.link_density * 35)
        )
        if _has_repeated_link_block(node):
            score -= 12
        candidates.append((score, metrics.text_length, node))
    if not candidates:
        fragment = BeautifulSoup(str(soup), "html.parser")
        _clean_fragment(fragment)
        return fragment

    node = max(candidates, key=lambda item: (item[0], item[1]))[2]
    fragment = BeautifulSoup(str(node), "html.parser")
    _clean_fragment(fragment)
    return fragment


def _html_to_markdown(html: str) -> tuple[str, _Metrics, bool, bool]:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(_REMOVED_HTML_TAGS):
        node.decompose()
    raw_metrics = _metrics(soup, include_boilerplate=True)
    fragment = _select_content_fragment(soup)
    markdown = _fragment_to_markdown(fragment)

    text_length = len(re.sub(r"\s+", "", markdown))
    link_text_length = _link_text(markdown)
    paragraph_count = len(fragment.find_all("p"))
    metrics = _Metrics(
        text_length=text_length,
        link_text_length=link_text_length,
        paragraph_count=paragraph_count,
        heading_count=len(fragment.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])),
        pre_count=len(fragment.find_all("pre")),
    )
    stripped = re.sub(r"\s+", "", _LINK_MARKDOWN.sub(r"", markdown))
    empty_content = not stripped
    removed_boilerplate = empty_content and raw_metrics.text_length > 0
    if removed_boilerplate:
        metrics = raw_metrics
        link_text_length = raw_metrics.link_text_length
        text_length = raw_metrics.text_length
    link_density = link_text_length / max(text_length, 1)
    navigation_only = (
        text_length > 0
        and link_text_length >= max(18, text_length // 2)
        and link_density >= 0.6
    )
    prose = re.sub(r"\s+", " ", _LINK_MARKDOWN.sub(" ", markdown)).strip()
    if (
        text_length > 0
        and (not empty_content or removed_boilerplate)
        and paragraph_count == 0
        and metrics.heading_count == 0
        and metrics.pre_count == 0
        and len(prose) < 24
    ):
        navigation_only = True
    if (
        text_length > 0
        and metrics.heading_count == 0
        and metrics.pre_count == 0
        and len(prose) < 400
        and _has_repeated_link_block(fragment)
    ):
        navigation_only = True
    return markdown, metrics, empty_content, navigation_only


def extract_content(body: bytes, content_type: str, *, offset: int = 0) -> ExtractedContent:
    if offset < 0:
        raise FetchError("URL_INVALID", "offset must be non-negative")
    media_type = _media_type(content_type)
    if media_type not in _TEXTUAL_TYPES:
        return ExtractedContent(None, media_type, binary=True, offset=offset)

    text = decode_text(body, content_type)
    metrics = _Metrics()
    empty_content = False
    navigation_only = False
    if media_type in {"text/html", "application/xhtml+xml"}:
        text, metrics, empty_content, navigation_only = _html_to_markdown(text)
        output_type = "text/markdown"
    elif media_type == "application/json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError as exc:
            raise FetchError("DECODE_FAILED", "Invalid JSON response") from exc
        output_type = "application/json"
        metrics = _Metrics(text_length=len(text))
    else:
        output_type = media_type
        metrics = _Metrics(text_length=len(text), paragraph_count=1 if text.strip() else 0)
    output = text[offset:]
    return ExtractedContent(
        output,
        output_type,
        binary=False,
        offset=offset,
        text_length=metrics.text_length,
        link_text_length=metrics.link_text_length,
        paragraph_count=metrics.paragraph_count,
        empty_content=empty_content or not bool(text.strip()),
        navigation_only=navigation_only,
    )
