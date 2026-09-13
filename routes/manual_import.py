from __future__ import annotations

"""새담메뉴얼 외부파일 등록(임포트) 파서.

지원 형식
    .txt / .md / .markdown : 마크다운 형태의 텍스트
    .html / .htm           : h1/h2 구조의 HTML 문서
    .docx                  : Word 문서(제목 스타일 기준으로 목차 분리)

모든 파서는 다음 구조를 돌려줍니다.

    {
        "title": "메뉴얼 제목",
        "description": "메뉴얼 설명",
        "sections": [
            {"title": "1. 목차", "description": "", "content_html": "<p>...</p>"},
            ...
        ],
    }

content_html 은 아직 살균(sanitize)되지 않은 값이므로
routes/manual.py 의 _sanitize_html() 을 반드시 통과시킨 뒤 저장합니다.
"""

import base64
import html as html_lib
import re
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------
# 공통 상수
# ---------------------------------------------------------------------
SUPPORTED_IMPORT_EXTENSIONS = {"txt", "md", "markdown", "html", "htm", "docx"}
IMPORT_ACCEPT_ATTR = ".txt,.md,.markdown,.html,.htm,.docx"

TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp949", "euc-kr", "utf-16")

CALLOUT_LABELS = {"note": "안내", "warn": "주의", "danger": "경고"}
CALLOUT_ALIASES = {
    "note": "note", "info": "note", "tip": "note", "안내": "note", "참고": "note", "팁": "note",
    "warn": "warn", "warning": "warn", "caution": "warn", "주의": "warn",
    "danger": "danger", "important": "danger", "경고": "danger", "금지": "danger",
}

IMAGE_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/pjpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

FIG_TOKEN_START = "\x00FIG"
FIG_TOKEN_END = "\x00"


class ManualImportError(Exception):
    """사용자에게 그대로 보여줄 수 있는 임포트 오류."""


def file_extension(filename: str) -> str:
    return Path(str(filename or "")).suffix.lower().lstrip(".")


def decode_text(data: bytes) -> str:
    for enc in TEXT_ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ManualImportError("파일의 문자 인코딩을 읽을 수 없습니다. UTF-8로 저장한 뒤 다시 시도해 주세요.")


def _esc(value: str) -> str:
    return html_lib.escape(str(value or ""), quote=True)


# ---------------------------------------------------------------------
# 이미지 처리
# ---------------------------------------------------------------------
_DATA_URI_RE = re.compile(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", re.I | re.S)


def _save_data_uri(src: str, image_saver, alt: str = "") -> str:
    """data:image/...;base64,... 를 실제 파일로 저장하고 URL을 돌려줍니다."""
    if not image_saver:
        return ""
    m = _DATA_URI_RE.match(src.strip())
    if not m:
        return ""
    ext = IMAGE_MIME_EXT.get(m.group(1).lower(), "")
    if not ext:
        return ""
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except Exception:
        return ""
    if not raw:
        return ""
    return image_saver(raw, ext, alt or ("imported." + ext)) or ""


def _normalize_src(src: str, image_saver, alt: str = "") -> str:
    src = (src or "").strip()
    if not src:
        return ""
    if src.lower().startswith("data:"):
        return _save_data_uri(src, image_saver, alt)
    if re.match(r"^(https?:)?//", src, re.I) or src.startswith("/"):
        return src
    # 로컬 상대경로(예: images/step1.png)는 서버에 파일이 없으므로 버립니다.
    return ""


def _figure_html(url: str, alt: str = "", caption: str = "") -> str:
    if not url:
        return ""
    filename = url.rsplit("/", 1)[-1]
    caption_html = "<figcaption>" + _esc(caption) + "</figcaption>" if caption else ""
    return (
        '<figure class="manual-image image-size-100 image-align-center" '
        'data-filename="' + _esc(filename) + '">'
        '<img src="' + _esc(url) + '" alt="' + _esc(alt) + '" data-filename="' + _esc(filename) + '">'
        + caption_html + "</figure>"
    )


# ---------------------------------------------------------------------
# 블록 -> 문서 조립
# ---------------------------------------------------------------------
def _blocks_to_document(blocks, default_section_title: str = "내용") -> dict:
    """블록 목록을 {title, description, sections} 구조로 조립합니다.

    블록 형식: {"kind": "title"|"doc_desc"|"section"|"section_desc"|"html", ...}
    """
    title = ""
    description = ""
    sections = []
    current = None

    def flush():
        nonlocal current
        if current is None:
            return
        current["content_html"] = "\n".join(
            piece for piece in current.pop("_parts") if piece
        ).strip()
        sections.append(current)
        current = None

    for block in blocks:
        kind = block.get("kind")

        if kind == "title":
            if not title:
                title = block.get("text", "").strip()
                continue
            # 두 번째 최상위 제목부터는 목차로 취급합니다.
            kind = "section"

        if kind == "doc_desc":
            if not description:
                description = block.get("text", "").strip()
            continue

        if kind == "section":
            flush()
            current = {
                "title": block.get("text", "").strip() or "제목 없음",
                "description": "",
                "_parts": [],
            }
            continue

        if kind == "section_desc":
            if current is not None and not current["description"]:
                current["description"] = block.get("text", "").strip()
            continue

        fragment = block.get("html", "")
        if not fragment:
            continue
        if current is None:
            current = {"title": default_section_title, "description": "", "_parts": []}
        current["_parts"].append(fragment)

    flush()

    sections = [s for s in sections if s["title"].strip() or s["content_html"].strip()]
    if not sections:
        sections = [{"title": default_section_title, "description": "", "content_html": ""}]

    return {
        "title": title[:200],
        "description": description[:1000],
        "sections": sections,
    }


# ---------------------------------------------------------------------
# 마크다운 / TXT 파서
# ---------------------------------------------------------------------
_RE_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_RE_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)")
_RE_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_RE_UNDERLINE = re.compile(r"__([^_\n]+)__")
_RE_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
_RE_STRIKE = re.compile(r"~~([^~\n]+)~~")

_RE_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_RE_ORDERED = re.compile(r"^\s*\d+[.)]\s+")
_RE_BULLET = re.compile(r"^\s*[-*+]\s+")
_RE_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_RE_SINGLE_IMAGE = re.compile(r"^!\[([^\]]*)\]\(([^)\s]+)\)$")
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _inline_md(text: str, image_saver=None) -> str:
    """인라인 마크다운 문법을 HTML로 변환합니다."""
    value = _esc(text)
    stash = []

    def _stash_code(m):
        stash.append("<code>" + m.group(1) + "</code>")
        return "\x00S" + str(len(stash) - 1) + "\x00"

    value = _RE_INLINE_CODE.sub(_stash_code, value)

    def _img(m):
        url = _normalize_src(html_lib.unescape(m.group(2)), image_saver, m.group(1))
        if not url:
            return ""
        stash.append('<img src="' + _esc(url) + '" alt="' + _esc(m.group(1)) + '">')
        return "\x00S" + str(len(stash) - 1) + "\x00"

    value = _RE_IMAGE.sub(_img, value)

    def _link(m):
        stash.append(
            '<a href="' + m.group(2) + '" target="_blank" rel="noopener">' + m.group(1) + "</a>"
        )
        return "\x00S" + str(len(stash) - 1) + "\x00"

    value = _RE_LINK.sub(_link, value)

    value = _RE_BOLD.sub(r"<strong>\1</strong>", value)
    value = _RE_UNDERLINE.sub(r"<u>\1</u>", value)
    value = _RE_ITALIC.sub(r"<em>\1</em>", value)
    value = _RE_STRIKE.sub(r"<s>\1</s>", value)

    for idx, piece in enumerate(stash):
        value = value.replace("\x00S" + str(idx) + "\x00", piece)
    return value


def _md_table(rows, image_saver) -> str:
    cells = []
    for row in rows:
        line = row.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|"):
            line = line[:-1]
        cells.append([c.strip() for c in line.split("|")])

    if not cells:
        return ""

    head, body = cells[0], cells[1:]
    out = ['<table class="manual-edit-table"><tbody>']
    out.append(
        "<tr>" + "".join("<th>" + _inline_md(c, image_saver) + "</th>" for c in head) + "</tr>"
    )
    for row in body:
        out.append(
            "<tr>" + "".join("<td>" + _inline_md(c, image_saver) + "</td>" for c in row) + "</tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


def _md_callout(lines, image_saver) -> str:
    """> 로 시작하는 인용 블록을 안내/주의/경고 박스 또는 인용문으로 변환합니다."""
    body = [re.sub(r"^\s*>\s?", "", ln) for ln in lines]
    while body and not body[0].strip():
        body.pop(0)
    if not body:
        return ""

    kind = ""
    label = ""
    marker = re.match(r"^\s*\[!\s*([^\]]+)\]\s*(.*)$", body[0])
    if marker:
        kind = CALLOUT_ALIASES.get(marker.group(1).strip().lower(), "")
        if kind:
            label = marker.group(2).strip() or CALLOUT_LABELS[kind]
            body = body[1:]

    inner = "<br>".join(_inline_md(ln, image_saver) for ln in body if ln.strip())

    if not kind:
        return "<blockquote>" + inner + "</blockquote>" if inner else ""

    parts = ['<div class="callout ' + kind + '"><strong>' + _esc(label) + "</strong>"]
    if inner:
        parts.append("<br>" + inner)
    parts.append("</div>")
    return "".join(parts)


def parse_markdown(text: str, image_saver=None) -> dict:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = []
    idx = 0
    total = len(lines)
    list_buffer = []
    list_kind = None
    awaiting_doc_desc = False
    awaiting_section_desc = False

    def close_list():
        nonlocal list_buffer, list_kind
        if list_buffer:
            items = "".join("<li>" + item + "</li>" for item in list_buffer)
            blocks.append({"kind": "html", "html": "<" + list_kind + ">" + items + "</" + list_kind + ">"})
        list_buffer = []
        list_kind = None

    while idx < total:
        line = lines[idx].rstrip()
        stripped = line.strip()

        # 코드블록
        if stripped.startswith("```") or stripped.startswith("~~~"):
            close_list()
            fence = stripped[:3]
            idx += 1
            code_lines = []
            while idx < total and not lines[idx].strip().startswith(fence):
                code_lines.append(lines[idx])
                idx += 1
            idx += 1
            code = _esc("\n".join(code_lines))
            blocks.append({"kind": "html", "html": "<pre><code>" + code + "</code></pre>"})
            awaiting_doc_desc = awaiting_section_desc = False
            continue

        # 인용 / 안내·주의·경고 박스
        if stripped.startswith(">"):
            close_list()
            quote_lines = []
            while idx < total and lines[idx].strip().startswith(">"):
                quote_lines.append(lines[idx])
                idx += 1
            fragment = _md_callout(quote_lines, image_saver)
            if fragment:
                blocks.append({"kind": "html", "html": fragment})
            awaiting_doc_desc = awaiting_section_desc = False
            continue

        # 표
        if stripped.startswith("|") and idx + 1 < total and _RE_TABLE_DIVIDER.match(lines[idx + 1]):
            close_list()
            table_rows = [lines[idx]]
            idx += 2
            while idx < total and lines[idx].strip().startswith("|"):
                table_rows.append(lines[idx])
                idx += 1
            fragment = _md_table(table_rows, image_saver)
            if fragment:
                blocks.append({"kind": "html", "html": fragment})
            awaiting_doc_desc = awaiting_section_desc = False
            continue

        # 빈 줄
        if not stripped:
            close_list()
            idx += 1
            continue

        # 제목 바로 다음의 ": 설명" 줄
        if stripped.startswith(": ") and (awaiting_doc_desc or awaiting_section_desc):
            close_list()
            blocks.append(
                {
                    "kind": "doc_desc" if awaiting_doc_desc else "section_desc",
                    "text": stripped[2:].strip(),
                }
            )
            awaiting_doc_desc = awaiting_section_desc = False
            idx += 1
            continue

        awaiting_doc_desc = awaiting_section_desc = False

        # 제목
        heading = _RE_HEADING.match(stripped)
        if heading:
            close_list()
            level = len(heading.group(1))
            content = heading.group(2).strip()
            if level == 1:
                blocks.append({"kind": "title", "text": content})
                awaiting_doc_desc = True
            elif level == 2:
                blocks.append({"kind": "section", "text": content})
                awaiting_section_desc = True
            elif level == 3:
                blocks.append(
                    {"kind": "html", "html": '<h3 class="sub">' + _inline_md(content, image_saver) + "</h3>"}
                )
            else:
                blocks.append({"kind": "html", "html": "<h4>" + _inline_md(content, image_saver) + "</h4>"})
            idx += 1
            continue

        # 구분선
        if _RE_HR.match(stripped):
            close_list()
            blocks.append({"kind": "html", "html": "<hr>"})
            idx += 1
            continue

        # 한 줄 전체가 이미지인 경우
        single_image = _RE_SINGLE_IMAGE.match(stripped)
        if single_image:
            close_list()
            url = _normalize_src(single_image.group(2), image_saver, single_image.group(1))
            fragment = _figure_html(url, single_image.group(1), single_image.group(1))
            if fragment:
                blocks.append({"kind": "html", "html": fragment})
            idx += 1
            continue

        # 목록
        if _RE_BULLET.match(line):
            if list_kind != "ul":
                close_list()
                list_kind = "ul"
            list_buffer.append(_inline_md(_RE_BULLET.sub("", line, count=1).strip(), image_saver))
            idx += 1
            continue

        if _RE_ORDERED.match(line):
            if list_kind != "ol":
                close_list()
                list_kind = "ol"
            list_buffer.append(_inline_md(_RE_ORDERED.sub("", line, count=1).strip(), image_saver))
            idx += 1
            continue

        close_list()
        blocks.append({"kind": "html", "html": "<p>" + _inline_md(stripped, image_saver) + "</p>"})
        idx += 1

    close_list()
    return _blocks_to_document(blocks)


# ---------------------------------------------------------------------
# HTML 파서
# ---------------------------------------------------------------------
_DROP_TAGS = ("script", "style", "noscript", "link", "meta", "iframe", "object", "embed", "form", "svg")


def _bs4_text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _clean_fragment(node, image_saver, as_element: bool = False) -> str:
    """bs4 노드에서 이미지 data URI를 파일로 옮기고 HTML 문자열을 돌려줍니다."""
    is_image_node = getattr(node, "name", "") == "img"
    images = list(node.find_all("img"))
    if is_image_node:
        images.insert(0, node)

    for img in images:
        url = _normalize_src(img.get("src") or "", image_saver, img.get("alt") or "")
        if not url:
            if img is node:
                return ""
            img.decompose()
            continue

        if img is node:
            # 문단 밖에 홀로 있는 이미지는 figure로 감싸 편집기 도구가 붙게 합니다.
            alt = img.get("alt") or ""
            return _figure_html(url, alt, alt)

        img["src"] = url
        img["data-filename"] = url.rsplit("/", 1)[-1]
        figure = img.find_parent("figure")
        if figure is not None:
            classes = set(figure.get("class") or [])
            classes.add("manual-image")
            if not any(c.startswith("image-size-") for c in classes):
                classes.add("image-size-100")
            if not any(c.startswith("image-align-") for c in classes):
                classes.add("image-align-center")
            figure["class"] = sorted(classes)
            figure["data-filename"] = img["data-filename"]

    return str(node) if as_element else node.decode_contents()


def parse_html_document(text: str, image_saver=None) -> dict:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        return _parse_html_fallback(text, image_saver)

    soup = BeautifulSoup(text, "html.parser")

    title = ""
    description = ""

    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        description = str(meta["content"]).strip()

    if soup.title and soup.title.string:
        title = str(soup.title.string).strip()

    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()

    h1 = soup.find("h1")
    if h1 is not None:
        heading_text = _bs4_text(h1)
        if heading_text:
            title = heading_text
        lead = h1.find_next_sibling("p")
        if lead is not None and not description:
            classes = " ".join(lead.get("class") or [])
            if re.search(r"lead|desc|manual-description", classes):
                description = _bs4_text(lead)
                lead.decompose()
        h1.decompose()

    blocks = []
    section_nodes = soup.select("section.block, section.manual-block, section[data-manual-section]")

    if section_nodes:
        for node in section_nodes:
            heading = node.find(["h2", "h3"])
            sec_title = ""
            if heading is not None:
                badge = heading.find(class_=re.compile(r"badge|num"))
                if badge is not None:
                    badge.decompose()
                sec_title = _bs4_text(heading)
                heading.decompose()
            blocks.append({"kind": "section", "text": sec_title or "제목 없음"})

            desc_node = node.find("p", class_=re.compile(r"\bdesc\b"))
            if desc_node is not None:
                blocks.append({"kind": "section_desc", "text": _bs4_text(desc_node)})
                desc_node.decompose()

            blocks.append({"kind": "html", "html": _clean_fragment(node, image_saver)})
    else:
        container = soup.find("main") or soup.find("article") or soup.body or soup
        pending = []

        def flush_pending():
            if not pending:
                return
            fragment = "".join(pending)
            pending.clear()
            if fragment.strip():
                blocks.append({"kind": "html", "html": fragment})

        for child in list(container.children):
            name = getattr(child, "name", None)
            if name is None:
                chunk = str(child).strip()
                if chunk:
                    pending.append("<p>" + _esc(chunk) + "</p>")
                continue
            if name == "h2":
                flush_pending()
                badge = child.find(class_=re.compile(r"badge|num"))
                if badge is not None:
                    badge.decompose()
                blocks.append({"kind": "section", "text": _bs4_text(child) or "제목 없음"})
                continue
            pending.append(_clean_fragment(child, image_saver, as_element=True))
        flush_pending()

    if not any(b.get("kind") == "section" for b in blocks):
        # h2가 하나도 없으면 본문 전체를 한 목차로 담습니다.
        body_html = "\n".join(b.get("html", "") for b in blocks if b.get("kind") == "html")
        blocks = [
            {"kind": "section", "text": title or "내용"},
            {"kind": "html", "html": body_html},
        ]

    document = _blocks_to_document(blocks)
    if title:
        document["title"] = title[:200]
    if description:
        document["description"] = description[:1000]
    return document


def _parse_html_fallback(text: str, image_saver=None) -> dict:
    """bs4가 없을 때 사용하는 최소 파서."""
    body = re.search(r"<body[^>]*>(.*)</body>", text, re.I | re.S)
    content = body.group(1) if body else text
    content = re.sub(
        r"<\s*(script|style|noscript)[^>]*>.*?<\s*/\s*\1\s*>", "", content, flags=re.I | re.S
    )

    title = ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", content, re.I | re.S)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        content = content[: m.start()] + content[m.end():]
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        if m:
            title = m.group(1).strip()

    parts = re.split(r"<h2[^>]*>(.*?)</h2>", content, flags=re.I | re.S)
    blocks = []
    if len(parts) > 1:
        for i in range(1, len(parts), 2):
            blocks.append({"kind": "section", "text": re.sub(r"<[^>]+>", "", parts[i]).strip()})
            blocks.append({"kind": "html", "html": parts[i + 1] if i + 1 < len(parts) else ""})
    else:
        blocks.append({"kind": "section", "text": title or "내용"})
        blocks.append({"kind": "html", "html": content})

    document = _blocks_to_document(blocks)
    if title:
        document["title"] = title[:200]
    return document


# ---------------------------------------------------------------------
# DOCX 파서
# ---------------------------------------------------------------------
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

_DOCX_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
_RE_FIG_TOKEN = re.compile("\x00FIG([^\x00]*)\x00")


def _docx_style_level(style: str) -> int:
    """워드 단락 스타일에서 제목 수준(0=본문, 1~4=제목)을 판단합니다."""
    key = re.sub(r"[\s_-]", "", (style or "")).lower()
    if not key:
        return 0
    if key in ("title", "제목"):
        return 1
    m = re.match(r"^(?:heading|제목|머리글)(\d)$", key)
    if m:
        return min(int(m.group(1)), 4)
    return 0


def _docx_save_image(embed_id: str, rels, zf, image_saver) -> str:
    if not embed_id or not image_saver:
        return ""
    rel = rels.get(embed_id)
    if not rel:
        return ""
    target = str(rel.get("target", ""))
    if not target:
        return ""
    name = target.split("/")[-1]
    ext = Path(name).suffix.lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    if ext not in _DOCX_IMAGE_EXT:
        return ""
    for candidate in ("word/" + target.lstrip("/"), target.lstrip("/"), "word/media/" + name):
        try:
            raw = zf.read(candidate)
        except KeyError:
            continue
        return image_saver(raw, ext, name) or ""
    return ""


def _docx_wrap_run(text: str, bold: bool, italic: bool, under: bool) -> str:
    if bold:
        text = "<strong>" + text + "</strong>"
    if italic:
        text = "<em>" + text + "</em>"
    if under:
        text = "<u>" + text + "</u>"
    return text


def _docx_runs_html(node, rels, zf, image_saver) -> str:
    out = []
    # 같은 서식이 이어지는 run은 하나로 합쳐 <strong>이</strong><strong>력</strong> 같은 결과를 막습니다.
    pending_text = ""
    pending_format = None

    def flush_pending():
        nonlocal pending_text, pending_format
        if pending_text and pending_format is not None:
            out.append(_docx_wrap_run(pending_text, *pending_format))
        pending_text = ""
        pending_format = None

    for child in node:
        if child.tag == _W + "hyperlink":
            flush_pending()
            inner = _docx_runs_html(child, rels, zf, image_saver)
            target = str(rels.get(child.get(_R + "id", ""), {}).get("target", ""))
            if inner and target.startswith(("http://", "https://")):
                out.append('<a href="' + _esc(target) + '" target="_blank" rel="noopener">' + inner + "</a>")
            elif inner:
                out.append(inner)
            continue

        if child.tag != _W + "r":
            continue

        rpr = child.find(_W + "rPr")
        bold = italic = under = False
        if rpr is not None:
            bold = rpr.find(_W + "b") is not None
            italic = rpr.find(_W + "i") is not None
            under = rpr.find(_W + "u") is not None

        piece = []
        for run_child in child:
            if run_child.tag == _W + "t":
                piece.append(_esc(run_child.text or ""))
            elif run_child.tag == _W + "br":
                piece.append("<br>")
            elif run_child.tag == _W + "tab":
                piece.append(" ")
            elif run_child.tag in (_W + "drawing", _W + "pict", _W + "object"):
                for blip in run_child.iter(_A + "blip"):
                    url = _docx_save_image(blip.get(_R + "embed", ""), rels, zf, image_saver)
                    if url:
                        piece.append("\x00FIG" + url + "\x00")

        text = "".join(piece)
        if not text:
            continue

        fmt = (bold, italic, under)
        if pending_format is not None and pending_format != fmt:
            flush_pending()
        pending_format = fmt
        pending_text += text

    flush_pending()
    return "".join(out)


def _docx_table_html(tbl, rels, zf, image_saver) -> str:
    rows_html = []
    for row_index, row in enumerate(tbl.findall(_W + "tr")):
        cells = []
        for cell in row.findall(_W + "tc"):
            paragraphs = []
            for para in cell.findall(_W + "p"):
                text = _RE_FIG_TOKEN.sub("", _docx_runs_html(para, rels, zf, image_saver))
                if text.strip():
                    paragraphs.append(text)
            tag = "th" if row_index == 0 else "td"
            cells.append("<" + tag + ">" + "<br>".join(paragraphs) + "</" + tag + ">")
        if cells:
            rows_html.append("<tr>" + "".join(cells) + "</tr>")
    if not rows_html:
        return ""
    return '<table class="manual-edit-table"><tbody>' + "".join(rows_html) + "</tbody></table>"


def parse_docx(data: bytes, image_saver=None) -> dict:
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except Exception:
        raise ManualImportError("DOCX 파일을 열 수 없습니다. 파일이 손상되었는지 확인해 주세요.")

    with zf:
        try:
            document_xml = zf.read("word/document.xml")
        except KeyError:
            raise ManualImportError("DOCX 파일에서 본문을 찾을 수 없습니다.")

        rels = {}
        try:
            rel_root = ET.fromstring(zf.read("word/_rels/document.xml.rels"))
            for rel in rel_root:
                rels[rel.get("Id", "")] = {
                    "target": rel.get("Target", ""),
                    "type": rel.get("Type", ""),
                }
        except Exception:
            rels = {}

        try:
            root = ET.fromstring(document_xml)
        except ET.ParseError:
            raise ManualImportError("DOCX 본문을 해석할 수 없습니다.")

        body = root.find(_W + "body")
        if body is None:
            raise ManualImportError("DOCX 본문이 비어 있습니다.")

        blocks = []
        list_buffer = []
        list_kind = None

        def close_list():
            nonlocal list_buffer, list_kind
            if list_buffer:
                items = "".join("<li>" + item + "</li>" for item in list_buffer)
                blocks.append({"kind": "html", "html": "<" + list_kind + ">" + items + "</" + list_kind + ">"})
            list_buffer = []
            list_kind = None

        for node in body:
            if node.tag == _W + "tbl":
                close_list()
                fragment = _docx_table_html(node, rels, zf, image_saver)
                if fragment:
                    blocks.append({"kind": "html", "html": fragment})
                continue

            if node.tag != _W + "p":
                continue

            style = ""
            numbered = False
            ppr = node.find(_W + "pPr")
            if ppr is not None:
                style_node = ppr.find(_W + "pStyle")
                if style_node is not None:
                    style = style_node.get(_W + "val", "")
                numbered = ppr.find(_W + "numPr") is not None

            text = _docx_runs_html(node, rels, zf, image_saver)
            figures = _RE_FIG_TOKEN.findall(text)
            text = _RE_FIG_TOKEN.sub("", text).strip()

            level = _docx_style_level(style)
            if level and text:
                close_list()
                plain = re.sub(r"<[^>]+>", "", text)
                if level == 1:
                    blocks.append({"kind": "title", "text": plain})
                elif level == 2:
                    blocks.append({"kind": "section", "text": plain})
                elif level == 3:
                    blocks.append({"kind": "html", "html": '<h3 class="sub">' + text + "</h3>"})
                else:
                    blocks.append({"kind": "html", "html": "<h4>" + text + "</h4>"})
            elif numbered and text:
                kind = "ol" if "listnumber" in style.lower() else "ul"
                if list_kind != kind:
                    close_list()
                    list_kind = kind
                list_buffer.append(text)
            elif text:
                close_list()
                blocks.append({"kind": "html", "html": "<p>" + text + "</p>"})

            for url in figures:
                close_list()
                fragment = _figure_html(url)
                if fragment:
                    blocks.append({"kind": "html", "html": fragment})

        close_list()

    return _blocks_to_document(blocks)


# ---------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------
def parse_manual_file(filename: str, data: bytes, image_saver=None) -> dict:
    ext = file_extension(filename)
    if ext not in SUPPORTED_IMPORT_EXTENSIONS:
        raise ManualImportError("TXT, MD, HTML, DOCX 파일만 메뉴얼로 등록할 수 있습니다.")

    if ext == "docx":
        document = parse_docx(data, image_saver)
    elif ext in ("html", "htm"):
        document = parse_html_document(decode_text(data), image_saver)
    else:
        document = parse_markdown(decode_text(data), image_saver)

    if not document.get("title"):
        stem = Path(str(filename or "")).stem.strip()
        document["title"] = (stem or "가져온 메뉴얼")[:200]

    return document


# ---------------------------------------------------------------------
# 샘플 양식
# ---------------------------------------------------------------------
SAMPLE_MARKDOWN = """# 메뉴얼 제목을 여기에 적습니다
: 목록에 표시할 한 줄 설명을 적습니다.

## 1. 기능 안내
: 이 목차에 대한 한 줄 설명입니다. (생략 가능)

이 줄은 본문 문단이 됩니다. **굵게**, *기울임*, __밑줄__, `코드` 를 쓸 수 있습니다.

### 소제목은 '###' 로 적습니다

- 점 목록 1
- 점 목록 2

1. 번호 목록 1
2. 번호 목록 2

> [!안내] 안내
> 안내 상자로 표시됩니다.

> [!주의] 주의
> 주의 상자로 표시됩니다.

> [!경고] 경고
> 경고 상자로 표시됩니다.

| 항목 | 내용 |
| --- | --- |
| 첫 번째 | 표는 이렇게 작성합니다. |
| 두 번째 | 등록 후 편집 화면에서 행과 열을 추가할 수 있습니다. |

```
코드 또는 입력 예시는 이렇게 적습니다.
```

## 2. 두 번째 목차
: '## ' 로 시작하는 줄마다 새 목차가 만들어집니다.

등록한 뒤에는 메뉴얼 편집 화면에서 글자색과 글자크기 변경, 이미지 추가,
표 편집, 목차 순서 변경을 그대로 사용할 수 있습니다.
"""


# ---------------------------------------------------------------------
# 마크다운 내보내기 (편집한 메뉴얼 -> .md 파일)
# ---------------------------------------------------------------------
_MD_ESCAPE_RE = re.compile(r"([\`*_\[\]])")


def _md_escape(text: str) -> str:
    return _MD_ESCAPE_RE.sub(r"\\1", text)


def _md_inline(node) -> str:
    """bs4 노드의 인라인 내용을 마크다운 문자열로 바꿉니다."""
    from bs4 import NavigableString, Tag  # type: ignore

    out = []
    for child in getattr(node, "children", []):
        if isinstance(child, NavigableString):
            text = str(child)
            if not text.strip():
                out.append(" " if text else "")
                continue
            out.append(_md_escape(re.sub(r"\s+", " ", text)))
            continue

        if not isinstance(child, Tag):
            continue

        name = (child.name or "").lower()

        if name == "br":
            out.append("\n")
        elif name in ("strong", "b"):
            inner = _md_inline(child).strip()
            out.append("**" + inner + "**" if inner else "")
        elif name in ("em", "i"):
            inner = _md_inline(child).strip()
            out.append("*" + inner + "*" if inner else "")
        elif name == "u":
            inner = _md_inline(child).strip()
            out.append("__" + inner + "__" if inner else "")
        elif name in ("s", "strike", "del"):
            inner = _md_inline(child).strip()
            out.append("~~" + inner + "~~" if inner else "")
        elif name == "code":
            out.append("`" + child.get_text() + "`")
        elif name == "a":
            inner = _md_inline(child).strip()
            href = str(child.get("href") or "").strip()
            out.append("[" + inner + "](" + href + ")" if href else inner)
        elif name == "img":
            src = str(child.get("src") or "").strip()
            alt = str(child.get("alt") or "").strip()
            out.append("![" + alt + "](" + src + ")" if src else "")
        else:
            out.append(_md_inline(child))

    return "".join(out)


def _md_out_cell(node) -> str:
    return _md_inline(node).replace("\n", " ").replace("|", r"\|").strip()


def _md_out_table(table) -> str:
    rows = table.find_all("tr")
    if not rows:
        return ""

    matrix = []
    for row in rows:
        cells = row.find_all(["th", "td"])
        matrix.append([_md_out_cell(c) for c in cells])

    width = max(len(r) for r in matrix)
    matrix = [r + [""] * (width - len(r)) for r in matrix]

    lines = ["| " + " | ".join(matrix[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for row in matrix[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _md_out_list(node, ordered: bool) -> str:
    lines = []
    for index, item in enumerate(node.find_all("li", recursive=False), start=1):
        text = _md_inline(item).replace("\n", " ").strip()
        if not text:
            continue
        lines.append((f"{index}. " if ordered else "- ") + text)
    return "\n".join(lines)


def _md_out_callout(node) -> str:
    classes = [c.lower() for c in (node.get("class") or [])]
    kind = next((k for k in ("note", "warn", "danger") if k in classes), "note")

    label = ""
    strong = node.find("strong")
    if strong is not None:
        label = strong.get_text(" ", strip=True)
        strong.extract()

    body = _md_inline(node).strip()
    body = re.sub(r"\n{2,}", "\n", body)

    head = "> [!" + CALLOUT_LABELS[kind] + "] " + (label or CALLOUT_LABELS[kind])
    lines = [head]
    for line in [ln.strip() for ln in body.split("\n")]:
        if line:
            lines.append("> " + line)
    return "\n".join(lines)


def _md_blocks(container) -> list:
    from bs4 import NavigableString, Tag  # type: ignore

    blocks = []
    for child in getattr(container, "children", []):
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                blocks.append(_md_escape(text))
            continue
        if not isinstance(child, Tag):
            continue

        name = (child.name or "").lower()
        classes = [c.lower() for c in (child.get("class") or [])]

        if name in ("h2", "h3"):
            text = _md_inline(child).strip()
            if text:
                blocks.append("### " + text)
        elif name in ("h4", "h5", "h6"):
            text = _md_inline(child).strip()
            if text:
                blocks.append("#### " + text)
        elif name == "p":
            text = _md_inline(child).strip()
            if text:
                blocks.append(text)
        elif name == "ul":
            text = _md_out_list(child, ordered=False)
            if text:
                blocks.append(text)
        elif name == "ol":
            text = _md_out_list(child, ordered=True)
            if text:
                blocks.append(text)
        elif name == "table":
            text = _md_out_table(child)
            if text:
                blocks.append(text)
        elif name == "pre":
            blocks.append("```\n" + child.get_text().rstrip() + "\n```")
        elif name == "hr":
            blocks.append("---")
        elif name == "figure":
            img = child.find("img")
            if img is not None:
                src = str(img.get("src") or "").strip()
                caption = child.find("figcaption")
                alt = (caption.get_text(" ", strip=True) if caption else "") or str(img.get("alt") or "")
                if src:
                    blocks.append("![" + alt.strip() + "](" + src + ")")
        elif name == "img":
            src = str(child.get("src") or "").strip()
            if src:
                blocks.append("![" + str(child.get("alt") or "").strip() + "](" + src + ")")
        elif name == "blockquote":
            text = _md_inline(child).strip()
            if text:
                blocks.append("\n".join("> " + ln.strip() for ln in text.split("\n") if ln.strip()))
        elif name == "div" and "callout" in classes:
            text = _md_out_callout(child)
            if text:
                blocks.append(text)
        else:
            blocks.extend(_md_blocks(child))

    return blocks


def html_to_markdown(content_html: str) -> str:
    """메뉴얼 본문 HTML을 다시 마크다운으로 바꿉니다."""
    if not (content_html or "").strip():
        return ""
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except Exception:
        text = re.sub(r"<br\s*/?>", "\n", content_html, flags=re.I)
        text = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return html_lib.unescape(text).strip()

    soup = BeautifulSoup(content_html, "html.parser")
    blocks = [b.strip() for b in _md_blocks(soup) if b and b.strip()]
    return "\n\n".join(blocks)


def manual_to_markdown(manual, sections) -> str:
    """메뉴얼 한 건을 다시 등록 가능한 .md 문서로 만듭니다."""
    lines = ["# " + str(manual.get("title") or "제목 없는 메뉴얼").strip()]

    description = str(manual.get("description") or "").strip()
    if description:
        lines.append(": " + description)

    for section in sections or []:
        lines.append("")
        lines.append("## " + str(section.get("title") or "제목 없음").strip())

        sec_desc = str(section.get("description") or "").strip()
        if sec_desc:
            lines.append(": " + sec_desc)

        body = html_to_markdown(str(section.get("content_html") or ""))
        if body:
            lines.append("")
            lines.append(body)

    return "\n".join(lines).rstrip() + "\n"
