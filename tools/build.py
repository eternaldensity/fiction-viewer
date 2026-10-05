#!/usr/bin/env python3
"""Static fiction site generator.

Reads .odt story documents grouped by directory (series) and emits a
fully static, navigable website with index pages and chapter pages.

Layout:
    stories/<series>/<story>.odt   source documents
    assets/style.css, assets/app.js source site chrome
    site/                           generated output (GitHub Pages artifact)

Series = immediate subdirectory of stories/.
Story  = one .odt file. Ordering + title derived from filename.
Chapters are detected from document text:
    - "Uni-Hack" + "Function N" (+ optional subtitle lines) -> Function chapters
    - "Chapter N" (+ optional subtitle line)                 -> Chapter chapters
Anything before the first detected chapter becomes "Opening".
Lines of only * or = that are not chapter boundaries become scene-break ornaments.

Formatting preservation (colours, sizes, emphasis, alignment) is done by
mapping ODT automatic styles to theme-aware CSS. Font families are
intentionally normalised to the site reading font for cohesion; everything
else (colour intent, relative size, bold/italic, centring, indents) is kept.

Only the Python standard library is used so the script runs in a bare
GitHub Actions runner with no pip installs.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORIES_ROOT = ROOT / "stories"
ASSETS_SRC = ROOT / "assets"
SITE_ROOT = ROOT / "site"

# Fall back to legacy layout where the series dir sits at repo root
# (e.g. uni-hack/*.odt) if stories/ does not exist yet.
LEGACY_FALLBACK = True

WORD_RE = re.compile(r"[A-Za-z0-9'\u2019\u00C0-\u024F]+")

NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
}


def q(ns: str, tag: str) -> str:
    return f"{{{NS[ns]}}}{tag}"


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"['\u2019]", "", value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "untitled"


def prettify_dirname(name: str) -> str:
    name = name.strip().replace("_", " ")
    # Preserve hyphens inside words (Uni-Hack), capitalise each part.
    words = name.split(" ")
    out_words = []
    for w in words:
        out_words.append("-".join(p[:1].upper() + p[1:] for p in w.split("-") if p))
    return " ".join(o for o in out_words if o) or name


def parse_story_filename(path: Path) -> tuple[int, str, str]:
    """Return (order, title, slug) derived from an .odt filename."""
    stem = path.stem.strip()
    order = 0
    title = stem
    # Patterns like "uh1 Unlocking", "uh2 unmasking", "01 - Title", "03_Title"
    m = re.match(r"^(?:[A-Za-z]*\s*0*(\d+))[\s\-_.:;]+(.+)$", stem)
    if m:
        try:
            order = int(m.group(1))
        except ValueError:
            order = 0
        title = m.group(2).strip(" -_.")
    else:
        m2 = re.match(r"^0*(\d+)[\s\-_.:;]+(.+)$", stem)
        if m2:
            order = int(m2.group(1))
            title = m2.group(2).strip(" -_.")
    if not title:
        title = stem
    # Normalise all-lowercase titles ("unmasking" -> "Unmasking").
    if title == title.lower():
        title = title[:1].upper() + title[1:]
    return order, title, slugify(title)


# ---------------------------------------------------------------------------
# ODT parsing
# ---------------------------------------------------------------------------

@dataclass
class ParaStyle:
    name: str
    color: str = ""
    font_size_pt: float = 0.0
    italic: bool = False
    bold: bool = False
    align: str = ""
    margin_left_cm: float = 0.0
    text_indent_cm: float = 0.0
    background: str = ""


@dataclass
class Para:
    style: str
    text: str
    html: str
    kind: str = "p"  # p | h
    level: int = 0


def _parse_length(value: str) -> tuple[float, str]:
    m = re.match(r"\s*([0-9.]+)\s*(pt|cm|mm|in|px|em)?\s*$", value or "")
    if not m:
        return 0.0, ""
    num = float(m.group(1))
    unit = m.group(2) or ""
    return num, unit


def _collect_styles(root: ET.Element) -> dict[str, ParaStyle]:
    styles: dict[str, ParaStyle] = {}
    auto = root.find(f".//{q('office','automatic-styles')}")
    containers = [auto] if auto is not None else [root]
    # Also look at top-level office:styles for named styles (Standard etc.)
    office_styles = root.find(f".//{q('office','styles')}")
    if office_styles is not None:
        containers.append(office_styles)
    for container in containers:
        if container is None:
            continue
        for st in container.findall(q("style", "style")):
            name = st.get(q("style", "name"), "")
            if not name or name in styles:
                continue
            ps = ParaStyle(name=name)
            for child in st:
                if child.tag == q("style", "paragraph-properties"):
                    for k, v in child.attrib.items():
                        local = k.split("}", 1)[1] if "}" in k else k
                        if local == "text-align":
                            ps.align = v
                        elif local == "margin-left":
                            num, unit = _parse_length(v)
                            if unit == "cm":
                                ps.margin_left_cm = num
                            elif unit == "mm":
                                ps.margin_left_cm = num / 10.0
                            elif unit == "in":
                                ps.margin_left_cm = num * 2.54
                        elif local == "text-indent":
                            num, unit = _parse_length(v)
                            if unit == "cm":
                                ps.text_indent_cm = num
                            elif unit == "mm":
                                ps.text_indent_cm = num / 10.0
                            elif unit == "in":
                                ps.text_indent_cm = num * 2.54
                elif child.tag == q("style", "text-properties"):
                    for k, v in child.attrib.items():
                        local = k.split("}", 1)[1] if "}" in k else k
                        if local == "color":
                            ps.color = v.lower()
                        elif local == "font-size":
                            num, unit = _parse_length(v)
                            if unit == "pt":
                                ps.font_size_pt = num
                        elif local == "font-style" and v == "italic":
                            ps.italic = True
                        elif local == "font-weight" and v in ("bold", "700"):
                            ps.bold = True
                        elif local == "background-color" and v not in ("transparent", "#ffffff", "#fff"):
                            # Only keep non-white backgrounds (rare).
                            if v.lower() not in ("#ffffff",):
                                ps.background = v
            styles[name] = ps
    return styles


def _inline_css_for(style: ParaStyle | None, is_span: bool = False) -> str:
    """Theme-aware inline CSS preserving colour intent, size and emphasis."""
    if style is None:
        return ""
    chunks: list[str] = []
    if style.color:
        c = style.color.lower()
        if c in ("#0000ff", "#000080", "blue"):
            chunks.append("color:var(--c-doc-blue)")
        elif c in ("#ff0000", "red"):
            chunks.append("color:var(--c-doc-red)")
        elif c in ("#808080", "gray", "grey"):
            chunks.append("color:var(--c-doc-grey)")
        elif c in ("#000000", "#222222", "#111111", "#333333"):
            pass  # inherit theme ink for readability in dark mode
        else:
            chunks.append(f"color:{style.color}")
    if style.font_size_pt:
        pt = style.font_size_pt
        if abs(pt - 12) < 0.6:
            pass
        elif pt <= 9.5:
            chunks.append("font-size:.86em")
        elif pt <= 11.5:
            chunks.append("font-size:.94em")
        elif pt <= 13:
            pass
        elif pt <= 15:
            chunks.append("font-size:1.24em")
        else:
            chunks.append(f"font-size:{pt / 12:.2f}em")
    if style.italic:
        chunks.append("font-style:italic")
    if style.bold:
        chunks.append("font-weight:700")
    if not is_span:
        if style.align == "center":
            chunks.append("text-align:center")
        elif style.align == "right":
            chunks.append("text-align:right")
        elif style.align == "justify":
            chunks.append("text-align:justify")
        if style.text_indent_cm >= 0.8:
            chunks.append("text-indent:2em")
        if style.margin_left_cm >= 0.8 and style.text_indent_cm < 0.8:
            chunks.append("margin-left:2em")
        if style.background:
            chunks.append(f"background-color:{style.background}")
    return ";".join(chunks)


def _is_blue(style: ParaStyle | None) -> bool:
    return bool(style and style.color.lower() in ("#0000ff", "#000080", "blue"))


def _render_children(elem: ET.Element, styles: dict[str, ParaStyle]) -> str:
    parts: list[str] = []
    if elem.text:
        parts.append(html.escape(elem.text))
    for child in elem:
        tag = child.tag
        if tag == q("text", "span"):
            sname = child.get(q("text", "style-name"), "")
            css = _inline_css_for(styles.get(sname), is_span=True)
            inner = _render_children(child, styles)
            if css:
                parts.append(f'<span style="{html.escape(css, quote=True)}">{inner}</span>')
            else:
                # Still keep emphasis-only spans semantic when detectable.
                st = styles.get(sname)
                if st and (st.italic or st.bold):
                    if st.italic and st.bold:
                        parts.append(f"<strong><em>{inner}</em></strong>")
                    elif st.italic:
                        parts.append(f"<em>{inner}</em>")
                    else:
                        parts.append(f"<strong>{inner}</strong>")
                else:
                    parts.append(inner)
        elif tag == q("text", "s"):
            n = child.get(q("text", "c"), "")
            try:
                count = int(n) if n else 1
            except ValueError:
                count = 1
            # <text:s/> is a single preserved space; c="N" adds N more.
            parts.append(" " * (count if n else 1))
            if child.tail:
                parts.append(html.escape(child.tail))
            continue
        elif tag == q("text", "tab"):
            parts.append('<span class="tab"></span>')
        elif tag == q("text", "line-break"):
            parts.append("<br>")
        elif tag == q("text", "soft-page-break"):
            pass
        elif tag == q("text", "a"):
            href = child.get(q("xlink", "href"), "") or child.get("href", "")
            inner = _render_children(child, styles)
            if href:
                parts.append(f'<a href="{html.escape(href, quote=True)}">{inner}</a>')
            else:
                parts.append(inner)
        else:
            # Unknown inline element: render its text content.
            parts.append(_render_children(child, styles))
        if child.tail and tag not in (q("text", "s"),):
            parts.append(html.escape(child.tail))
    return "".join(parts)


def parse_odt(path: Path) -> tuple[list[Para], dict[str, ParaStyle]]:
    with zipfile.ZipFile(path) as z:
        try:
            raw = z.read("content.xml")
        except KeyError:
            raise ValueError(f"{path} is not a valid ODT (content.xml missing)")
        root = ET.fromstring(raw)
    styles = _collect_styles(root)
    body = root.find(f".//{q('office','text')}")
    paras: list[Para] = []
    if body is None:
        return paras, styles
    for child in body:
        if child.tag == q("text", "p"):
            sname = child.get(q("text", "style-name"), "")
            text = "".join(child.itertext())
            # Normalise non-breaking / narrow spaces.
            text = text.replace("\u00a0", " ")
            inner = _render_children(child, styles)
            # Drop lone tab placeholders that produce empty markup.
            paras.append(Para(style=sname, text=text, html=inner, kind="p"))
        elif child.tag == q("text", "h"):
            sname = child.get(q("text", "style-name"), "")
            level = int(child.get(q("text", "outline-level"), "1") or "1")
            text = "".join(child.itertext()).replace("\u00a0", " ")
            inner = _render_children(child, styles)
            paras.append(Para(style=sname, text=text, html=inner, kind="h", level=level))
        # Lists / tables are not used in these documents; flatten anything else.
        elif child.tag == q("text", "list"):
            for item in child.iter(q("text", "p")):
                sname = item.get(q("text", "style-name"), "")
                text = "".join(item.itertext())
                inner = _render_children(item, styles)
                paras.append(Para(style=sname, text="\u2022 " + text, html="&#8226; " + inner, kind="p"))
    return paras, styles


# ---------------------------------------------------------------------------
# Chapter splitting
# ---------------------------------------------------------------------------

SEPARATOR_RE = re.compile(r"^[=\-_\u2500\u2014]{6,}\s*$")
STARS_RE = re.compile(r"^(\*\s*){2,}$|^\*{1,3}\s*$")
UNI_RE = re.compile(r"(?i)^uni[\s\-]?hack\s*:?\s*$")
FUNCTION_RE = re.compile(r"(?i)^function\s+(\d+)\s*$")

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}


def _roman_to_int(s: str) -> int:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    if not s or any(c not in vals for c in s):
        return 0
    total, prev = 0, 0
    for c in reversed(s):
        v = vals[c]
        total, prev = (total - v, v) if v < prev else (total + v, v)
    # Guard against non-canonical strings like "ID" or "CIVIC".
    if total <= 0 or total > 60:
        return 0
    return total


def _words_to_int(s: str) -> int:
    s = s.strip().lower().replace("-", " ")
    if s.isdigit():
        return int(s)
    if s in NUMBER_WORDS:
        return NUMBER_WORDS[s]
    parts = s.split()
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in NUMBER_WORDS:
        return _TENS[parts[0]] + NUMBER_WORDS[parts[1]]
    return 0


def parse_chapter_heading(text: str) -> tuple[int, str] | None:
    """Parse 'Chapter N' / 'Chapter One' with optional ': subtitle'.

    Returns (number, same_line_subtitle) or None. Number is 0 when the
    heading has no recognisable number (caller falls back to sequence).
    """
    m = re.match(r"(?i)^chapter\s+(.+?)\s*$", text.strip())
    if not m:
        return None
    rest = m.group(1).strip()
    # Split off a same-line subtitle: "One: Agents and Daemons".
    num_part, subtitle = rest, ""
    for sep in (":", "\u2014", "--"):
        if sep in rest:
            head, tail = rest.split(sep, 1)
            if tail.strip():
                num_part, subtitle = head.strip(), tail.strip()
                break
    n = _words_to_int(num_part)
    if not n:
        n = _roman_to_int(num_part)
    if not n and num_part:
        # Unnumbered heading like "Chapter One..." that failed to parse:
        # still a chapter, number resolved by sequence later.
        pass
    elif not n and not num_part:
        return None
    # Reject false positives: the "number" part must be short. A long
    # sentence starting with the word Chapter is body text, not a heading.
    if len(num_part) > 30:
        return None
    return n, subtitle


CHAPTER_RE = re.compile(r"(?i)^chapter\b")
VOLUME_TITLE_RE = re.compile(r"(?i)^uni[\s\-]?hack\s+volume\s+[ivx\d]+.*")


def _is_separator(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    nospace = re.sub(r"\s+", "", t)
    if SEPARATOR_RE.match(t) or (set(nospace) <= set("=") and len(nospace) >= 6):
        return True
    # Long runs of - / _ / em-dashes also count as separators.
    if set(nospace) <= set("-_\u2500\u2014") and len(nospace) >= 6:
        return True
    return False


def _is_scene_stars(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    nospace = re.sub(r"\s+", "", t)
    if set(nospace) == {"*"} and len(nospace) >= 1:
        return True
    return bool(STARS_RE.match(t))


@dataclass
class Chapter:
    slug: str
    label: str          # e.g. "Function 3" / "Chapter 2" / "Opening"
    title: str          # full display title
    subtitle: str = ""
    paras: list[Para] = field(default_factory=list)
    words: int = 0


def split_chapters(paras: list[Para], styles: dict[str, ParaStyle]) -> tuple[list[Chapter], str]:
    """Split flat paragraphs into chapters. Returns (chapters, doc_title_hint)."""
    # Detect doc-level title hint (first blue/large centred line for uh2).
    doc_hint = ""
    for p in paras[:4]:
        t = p.text.strip()
        if t and len(t) < 120 and ("Uni-Hack" in t or "Unmasking" in t):
            doc_hint = t
            break

    # Find chapter-start indices.
    starts: list[tuple[int, str, int, str]] = []  # (para_index, kind, number, same_line_subtitle)
    seq = 0
    i = 0
    n = len(paras)
    while i < n:
        t = paras[i].text.strip()
        if UNI_RE.match(t):
            # Look ahead for "Function N" skipping blanks/separators.
            j = i + 1
            while j < n and not paras[j].text.strip():
                j += 1
            if j < n and FUNCTION_RE.match(paras[j].text.strip()):
                num = FUNCTION_RE.match(paras[j].text.strip()).group(1)  # type: ignore
                starts.append((i, "function", int(num), ""))
                i = j + 1
                continue
        elif FUNCTION_RE.match(t):
            # Bare "Function N" without preceding Uni-Hack line (rare).
            num = FUNCTION_RE.match(t).group(1)  # type: ignore
            # Avoid false positives: require nearby separator or Uni-Hack context.
            prev_text = " ".join(p.text for p in paras[max(0, i - 4):i])
            if "Uni-Hack" in prev_text or _is_separator(paras[i - 1].text) if i > 0 else True:
                starts.append((i, "function", int(num), ""))
                i += 1
                continue
        elif len(t) <= 140:
            parsed = parse_chapter_heading(t)
            if parsed is not None:
                num, inline_sub = parsed
                seq += 1
                starts.append((i, "chapter", num or seq, inline_sub))
                i += 1
                continue
        i += 1

    chapters: list[Chapter] = []
    if not starts:
        # Single-chapter story.
        ch = Chapter(slug="full", label="Full text", title="Full text", paras=list(paras))
        chapters.append(ch)
        return chapters, doc_hint

    def collect_subtitle(idx: int, kind: str) -> tuple[str, int]:
        """Collect up to two short blue subtitle lines (uh1) or one short line (uh2)."""
        subs: list[str] = []
        j = idx
        # Skip the chapter marker line(s) themselves first.
        # Caller passes index just after the marker; here we gather subtitles.
        guard = 0
        while j < n and guard < 4:
            t = paras[j].text.strip()
            if not t:
                j += 1
                guard += 1
                continue
            if _is_separator(t) or UNI_RE.match(t) or FUNCTION_RE.match(t) or CHAPTER_RE.match(t):
                break
            if len(t) > 140:
                break
            st = styles.get(paras[j].style)
            if kind == "function":
                # Subtitles in uh1 are blue (P12/P14). Stop at first body-colour paragraph.
                if _is_blue(st):
                    subs.append(t)
                    j += 1
                    guard += 1
                    # Allow a second subtitle line (e.g. "{= != ==}" + "The First Quackers").
                    continue
                break
            else:
                # uh2 subtitles are short lines directly under "Chapter N".
                if len(t) <= 80 and guard == 0:
                    subs.append(t)
                    j += 1
                break
            guard += 1
        return (" \u2014 ".join(subs) if subs else ""), j

    # Opening: everything before first start (minus title-page separators).
    first_start = starts[0][0]
    opening_paras = [p for p in paras[:first_start] if p.text.strip() or p.html.strip()]
    # Drop leading/trailing separators / scene-breaks in opening.
    while opening_paras and (_is_separator(opening_paras[0].text) or _is_scene_stars(opening_paras[0].text)):
        # Keep at most one leading ornament; drop pure separators.
        if _is_separator(opening_paras[0].text):
            opening_paras.pop(0)
        else:
            break
    while opening_paras and (_is_separator(opening_paras[-1].text) or _is_scene_stars(opening_paras[-1].text)):
        opening_paras.pop()

    # If opening is only a title page (< 4 paras of real text), still keep it as Opening.
    has_opening = any(p.text.strip() for p in opening_paras)
    # Determine chapter boundaries.
    for si, (sidx, kind, num, inline_sub) in enumerate(starts):
        end = starts[si + 1][0] if si + 1 < len(starts) else n
        # Chapter marker spans sidx..marker_end (Uni-Hack line + Function line).
        marker_end = sidx + 1
        if kind == "function":
            # sidx may be the "Uni-Hack" line; Function line is next non-blank.
            j = sidx + 1
            while j < n and not paras[j].text.strip():
                j += 1
            if j < n and FUNCTION_RE.match(paras[j].text.strip()):
                marker_end = j + 1
            else:
                marker_end = sidx + 1
            label = f"Function {int(num)}"
        else:
            marker_end = sidx + 1
            label = f"Chapter {int(num)}"
        subtitle, body_start = collect_subtitle(marker_end, kind)
        if not subtitle:
            subtitle = inline_sub
        # Special case uh1 Function 13: author aside lines are body, not subtitle (handled: not blue).
        body_paras = [p for p in paras[body_start:end]]
        # Strip leading blanks/separators left over.
        while body_paras and (not body_paras[0].text.strip() or _is_separator(body_paras[0].text)):
            body_paras.pop(0)
        while body_paras and (not body_paras[-1].text.strip() or _is_separator(body_paras[-1].text)
                              or _is_scene_stars(body_paras[-1].text)):
            body_paras.pop()
        if kind == "function":
            slug = f"function-{int(num):02d}"
            title = f"Uni-Hack \u2014 {label}" if not subtitle else f"{label}"
        else:
            slug = f"chapter-{int(num):02d}"
            title = label
        chapters.append(Chapter(slug=slug, label=label, title=title, subtitle=subtitle, paras=body_paras))

    if has_opening:
        # Derive a friendly opening title.
        opening = Chapter(slug="opening", label="Opening", title="Opening", paras=opening_paras)
        # If the opening contains "The Inner Shell." use it as subtitle.
        for p in opening_paras:
            t = p.text.strip()
            if t in ("The Inner Shell.", "Turtle Revolutions, Segment 1:"):
                opening.subtitle = "The Inner Shell"
                break
        chapters.insert(0, opening)

    # Word counts.
    for ch in chapters:
        ch.words = sum(len(WORD_RE.findall(p.text)) for p in ch.paras)
    return chapters, doc_hint


def para_to_html(p: Para, styles: dict[str, ParaStyle], first_body: bool = False) -> str:
    t = p.text.strip()
    if not t:
        return ""
    if _is_scene_stars(t):
        return '<div class="ornament" aria-hidden="true"><span></span><i>*</i><i>*</i><i>*</i><span></span></div>'
    if _is_separator(t):
        return ""
    css = _inline_css_for(styles.get(p.style))
    cls = "story-p"
    st = styles.get(p.style)
    # Markers for CSS hooks (centred headings etc.)
    if st and st.align == "center":
        cls += " is-centred"
    if _is_blue(st):
        cls += " is-blue"
    if first_body:
        cls += " is-lede"
    # Long unindented body paragraphs get a tidy indent via CSS class.
    attr = f' style="{html.escape(css, quote=True)}"' if css else ""
    if p.kind == "h":
        level = min(max(p.level + 1, 2), 4)
        return f"<h{level} class=\"{cls}\"{attr}>{p.html or html.escape(t)}</h{level}>"
    inner = p.html if p.html.strip() else html.escape(t)
    return f'<p class="{cls}"{attr}>{inner}</p>'


def excerpt(paras: list[Para], limit: int = 220) -> str:
    words: list[str] = []
    for p in paras:
        if _is_separator(p.text) or _is_scene_stars(p.text):
            continue
        t = p.text.strip()
        if not t or len(t) < 40:
            continue
        # Skip meta one-liners and the legal disclaimer.
        if t.startswith("Disclaimer:"):
            continue
        if FUNCTION_RE.match(t) or UNI_RE.match(t):
            continue
        if len(t) <= 140 and parse_chapter_heading(t) is not None:
            continue
        words.extend(t.split())
        if len(" ".join(words)) >= limit:
            break
    s = " ".join(words)[:limit].rsplit(" ", 1)[0]
    return s + ("..." if s else "")


# ---------------------------------------------------------------------------
# Site model + rendering
# ---------------------------------------------------------------------------

@dataclass
class Story:
    series_id: str
    series_title: str
    order: int
    title: str
    slug: str
    source: Path
    chapters: list[Chapter] = field(default_factory=list)
    words: int = 0
    doc_hint: str = ""


@dataclass
class Series:
    id: str
    title: str
    stories: list[Story] = field(default_factory=list)
    words: int = 0


SITE_NAME = "The Cosy Nook"
SITE_TAGLINE = "A quiet shelf for long stories"


def discover_sources() -> list[tuple[str, Path]]:
    """Return [(series_id, odt_path)] discovered on disk.

    Only OpenDocument (.odt) sources are built. Anything else (e.g. Word
    .doc) is reported so the author knows to re-save it as ODF.
    """
    found: list[tuple[str, Path]] = []
    skipped: list[Path] = []

    def harvest(series_name: str, directory: Path) -> None:
        for item in sorted(directory.iterdir()):
            if item.is_dir():
                continue
            if item.suffix.lower() == ".odt":
                found.append((series_name, item))
            elif item.name.startswith((".", "~")) or item.suffix.lower() == ".tmp":
                continue
            else:
                skipped.append(item)

    if STORIES_ROOT.exists():
        for series_dir in sorted(p for p in STORIES_ROOT.iterdir() if p.is_dir()):
            harvest(series_dir.name, series_dir)
        # Also allow loose .odt directly under stories/ as its own series.
        for odt in sorted(STORIES_ROOT.glob("*.odt")) + sorted(STORIES_ROOT.glob("*.ODT")):
            found.append(("miscellany", odt))
    if not found and LEGACY_FALLBACK:
        for child in sorted(ROOT.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name not in (
                "site", "assets", "tools", "node_modules", "__pycache__",
            ):
                for odt in sorted(child.glob("*.odt")) + sorted(child.glob("*.ODT")):
                    found.append((child.name, odt))
    for path in skipped:
        print(f"WARNING: skipping {path}: not an .odt file "
              f"(save it as ODF to publish it)", file=sys.stderr)
    return found


def load_library() -> list[Series]:
    discovered = discover_sources()
    if not discovered:
        print("No .odt sources found.", file=sys.stderr)
        return []
    series_map: dict[str, Series] = {}
    for series_dirname, odt_path in discovered:
        sid = slugify(series_dirname)
        if sid not in series_map:
            series_map[sid] = Series(id=sid, title=prettify_dirname(series_dirname))
        series = series_map[sid]
        order, title, slug = parse_story_filename(odt_path)
        story = Story(
            series_id=sid,
            series_title=series.title,
            order=order or len(series.stories) + 1,
            title=title,
            slug=slug,
            source=odt_path,
        )
        try:
            paras, styles = parse_odt(odt_path)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: could not parse {odt_path}: {e}", file=sys.stderr)
            continue
        chapters, hint = split_chapters(paras, styles)
        # Render chapter HTML once and stash on the object.
        rendered: list[Chapter] = []
        for ch in chapters:
            body_parts: list[str] = []
            lede_done = False
            for p in ch.paras:
                is_body = bool(p.text.strip()) and not _is_separator(p.text) and not _is_scene_stars(p.text)
                use_lede = is_body and not lede_done and len(p.text.strip()) > 60
                out = para_to_html(p, styles, first_body=use_lede)
                if out:
                    body_parts.append(out)
                    if use_lede:
                        lede_done = True
            ch_html = "\n".join(body_parts)
            ch.words = sum(len(WORD_RE.findall(p.text)) for p in ch.paras)
            # Attach rendered html dynamically.
            ch.__dict__["body_html"] = ch_html  # type: ignore[attr-defined]
            rendered.append(ch)
        story.chapters = rendered
        story.words = sum(c.words for c in rendered)
        story.doc_hint = hint
        series.stories.append(story)
    # Sort stories + series, compute totals.
    out = sorted(series_map.values(), key=lambda s: s.title.lower())
    for s in out:
        s.stories.sort(key=lambda st: (st.order, st.title.lower()))
        s.words = sum(st.words for st in s.stories)
    return out


def reading_minutes(words: int) -> int:
    return max(1, round(words / 200))


# -- HTML shell -------------------------------------------------------------

def shell(*, title: str, asset_prefix: str, body: str, extra_head: str = "",
          body_class: str = "", prev_url: str = "", next_url: str = "") -> str:
    esc_title = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en" data-theme="light" data-size="m">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc_title} &middot; {SITE_NAME}</title>
<meta name="description" content="{html.escape(SITE_TAGLINE)}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;0,9..144,700;1,9..144,400;1,9..144,500&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{asset_prefix}assets/style.css">
{extra_head}
</head>
<body class="{body_class}" data-prev="{html.escape(prev_url, quote=True)}" data-next="{html.escape(next_url, quote=True)}">
<a class="skip" href="#main">Skip to content</a>
<div class="progress" aria-hidden="true"><span id="progressBar"></span></div>
<header class="topbar">
  <div class="wrap topbar-in">
    <a class="brand" href="{asset_prefix}index.html" aria-label="Home">
      <span class="brand-mark" aria-hidden="true"><span class="lamp"></span></span>
      <span class="brand-text"><strong>{SITE_NAME}</strong><small>{SITE_TAGLINE}</small></span>
    </a>
    <nav class="topnav" aria-label="Primary">
      <a href="{asset_prefix}index.html">Library</a>
      <button class="iconbtn" id="searchBtn" type="button" aria-label="Search">Search</button>
      <div class="reader-controls" role="group" aria-label="Reading settings">
        <button id="sizeDown" type="button" title="Smaller text" aria-label="Smaller text">A&minus;</button>
        <button id="sizeUp" type="button" title="Larger text" aria-label="Larger text">A+</button>
        <button id="themeBtn" type="button" title="Change theme" aria-label="Change theme"><span class="dot" aria-hidden="true"></span><span id="themeName">Light</span></button>
      </div>
    </nav>
  </div>
</header>
<div class="search-drawer" id="searchDrawer" hidden>
  <div class="wrap">
    <input id="searchInput" type="search" placeholder="Search titles, chapters..." aria-label="Search stories">
    <div id="searchOut" class="search-out" role="listbox" aria-label="Search results"></div>
  </div>
</div>
<main id="main">
{body}
</main>
<footer class="footer">
  <div class="wrap footer-in">
    <p><strong>{SITE_NAME}</strong> &mdash; {SITE_TAGLINE}. Built statically from source documents; push an edit and the shelf restacks itself.</p>
    <p class="fine">Settle in. Your place is remembered on this device only.</p>
  </div>
</footer>
<script src="{asset_prefix}assets/app.js" defer></script>
</body>
</html>
"""


def breadcrumbs(items: list[tuple[str, str]]) -> str:
    # items: [(label, href or "" for current)]
    bits = []
    for label, href in items:
        if href:
            bits.append(f'<a href="{html.escape(href)}">{html.escape(label)}</a>')
        else:
            bits.append(f'<span aria-current="page">{html.escape(label)}</span>')
    return '<nav class="crumbs" aria-label="Breadcrumb">' + '<span class="sep" aria-hidden="true">/</span>'.join(bits) + "</nav>"


def fmt_words(n: int) -> str:
    if n >= 1000:
        return f"{n:,}".replace(",", "\u2009")
    return str(n)


# -- Page builders -----------------------------------------------------------

def home_page(series_list: list[Series]) -> str:
    total_words = sum(s.words for s in series_list)
    total_chapters = sum(len(st.chapters) for s in series_list for st in s.stories)
    cards = []
    for s in series_list:
        story_rows = []
        for st in s.stories:
            ch_links = "".join(
                f'<a href="{s.id}/{st.slug}/{c.slug}.html">{html.escape(c.subtitle or c.label)}</a>'
                for c in st.chapters[:4]
            )
            story_rows.append(f"""
            <li class="storyline">
              <div class="storyline-main">
                <a class="storyline-title" href="{s.id}/{st.slug}/index.html">{html.escape(st.title)}</a>
                <span class="meta">{len(st.chapters)} chapters &middot; {fmt_words(st.words)} words &middot; ~{reading_minutes(st.words)} min</span>
              </div>
              <div class="storyline-sample">{ch_links}<span class="more">&rarr;</span></div>
            </li>""")
        cards.append(f"""
        <article class="series-card">
          <div class="card-spine" aria-hidden="true"></div>
          <div class="card-body">
            <p class="kicker">Series &middot; {len(s.stories)} stor{"y" if len(s.stories)==1 else "ies"}</p>
            <h2><a href="{s.id}/index.html">{html.escape(s.title)}</a></h2>
            <p class="lede">{fmt_words(s.words)} words across {sum(len(st.chapters) for st in s.stories)} chapters. Pull up a chair.</p>
            <ul class="storylines">{''.join(story_rows)}</ul>
            <p><a class="btn" href="{s.id}/index.html">Open the shelf</a></p>
          </div>
        </article>""")
    body = f"""
<div class="wrap">
  <section class="hero">
    <div class="hero-book" aria-hidden="true">
      <div class="hero-pages"><span></span><span></span><span></span><span></span><span></span></div>
      <div class="hero-glow"></div>
    </div>
    <div class="hero-text">
      <p class="kicker">A small static library &middot; {len(series_list)} series &middot; {total_chapters} chapters &middot; {fmt_words(total_words)} words</p>
      <h1>Stories kept<br><em>warm</em> for late reading.</h1>
      <p class="lede">Neat shelves, honest typography, no noise. Pick a series, follow the chapters, and your lamp stays on where you left it.</p>
      <div class="hero-actions">
        <a class="btn solid" href="#shelves">Browse the shelves</a>
        <button class="btn ghost" id="continueBtn" type="button" hidden>Continue where you left off</button>
      </div>
      <ul class="hero-notes">
        <li>Grouped by series, indexed by chapter</li>
        <li>Remembers theme, size &amp; place</li>
        <li>Reads well on phones &amp; e-ink</li>
      </ul>
    </div>
  </section>
  <section id="shelves" class="shelves">
    {''.join(cards)}
  </section>
  <section class="colophon">
    <h2>How this shelf works</h2>
    <ol>
      <li><strong>Series are folders.</strong> Add <code>stories/my-series/my-story.odt</code> and it appears as a new shelf.</li>
      <li><strong>Chapters are headings.</strong> <code>Function N</code> or <code>Chapter N</code> lines (with an optional subtitle underneath) become chapter pages.</li>
      <li><strong>Push to publish.</strong> TheAction rebuilds every page, so editing a document and pushing is all it takes.</li>
    </ol>
  </section>
</div>"""
    # Fix the em-dash typo guard above ("TheAction" -> "The action").
    body = body.replace("TheAction rebuilds", "The action rebuilds")
    return shell(title="Library", asset_prefix="", body=body, body_class="page-home")


def series_page(s: Series) -> str:
    stories_html = []
    for st in s.stories:
        ch_items = []
        for idx, c in enumerate(st.chapters, 1):
            sub = f'<span class="ch-sub">{html.escape(c.subtitle)}</span>' if c.subtitle else ""
            ch_items.append(f"""
            <li><a href="{st.slug}/{c.slug}.html">
              <span class="ch-num">{idx:02d}</span>
              <span class="ch-main"><strong>{html.escape(c.label)}</strong>{sub}</span>
              <span class="ch-words">{fmt_words(c.words)} w</span>
            </a></li>""")
        stories_html.append(f"""
        <article class="story-block" id="{st.slug}">
          <header class="story-head">
            <div>
              <p class="kicker">Story {st.order} &middot; {len(st.chapters)} chapters &middot; ~{reading_minutes(st.words)} min</p>
              <h2><a href="{st.slug}/index.html">{html.escape(st.title)}</a></h2>
              <p class="lede">{html.escape(excerpt(st.chapters[0].paras) if st.chapters else '')}</p>
            </div>
            <a class="btn solid" href="{st.slug}/{st.chapters[0].slug}.html">Start reading</a>
          </header>
          <ol class="chapter-index">{''.join(ch_items)}</ol>
        </article>""")
    body = f"""
<div class="wrap narrow">
  {breadcrumbs([("Library", "../index.html"), (s.title, "")])}
  <header class="page-head">
    <p class="kicker">Series</p>
    <h1>{html.escape(s.title)}</h1>
    <p class="lede">{len(s.stories)} stor{"y" if len(s.stories)==1 else "ies"} &middot; {fmt_words(s.words)} words &middot; grouped on this shelf by folder.</p>
  </header>
  {''.join(stories_html)}
</div>"""
    return shell(title=s.title, asset_prefix="../", body=body, body_class="page-series")


def story_page(s: Series, st: Story, prev_story: Story | None, next_story: Story | None) -> str:
    ch_items = []
    for idx, c in enumerate(st.chapters, 1):
        sub = f'<span class="ch-sub">{html.escape(c.subtitle)}</span>' if c.subtitle else ""
        ch_items.append(f"""
        <li><a href="{c.slug}.html">
          <span class="ch-num">{idx:02d}</span>
          <span class="ch-main"><strong>{html.escape(c.label)}</strong>{sub}</span>
          <span class="ch-words">{fmt_words(c.words)} words</span>
        </a></li>""")
    sib_nav = ""
    if prev_story or next_story:
        sib_nav = '<nav class="sibnav">' + "".join([
            f'<a href="../{prev_story.slug}/index.html">&larr; {html.escape(prev_story.title)}&nbsp; ' if prev_story else "",
            f'<a class="right" href="../{next_story.slug}/index.html">{html.escape(next_story.title)} &rarr;</a>' if next_story else "",
        ]) + "</nav>"
    first = st.chapters[0].slug + ".html" if st.chapters else "#"
    begin_label = "Begin reading" if len(st.chapters) <= 1 else "Begin at the opening"
    body = f"""
<div class="wrap narrow">
  {breadcrumbs([("Library", "../../index.html"), (s.title, "../index.html"), (st.title, "")])}
  <header class="page-head story-hero">
    <p class="kicker">Series: {html.escape(s.title)} &middot; Story {st.order}</p>
    <h1>{html.escape(st.title)}</h1>
    <p class="lede">{len(st.chapters)} chapters &middot; {fmt_words(st.words)} words &middot; about {reading_minutes(st.words)} minutes by lamplight.</p>
    <div class="hero-actions"><a class="btn solid" href="{first}">{begin_label}</a></div>
  </header>
  <ol class="chapter-index tall">{''.join(ch_items)}</ol>
  {sib_nav}
</div>"""
    return shell(title=f"{st.title} ({s.title})", asset_prefix="../../", body=body, body_class="page-story")


def chapter_page(s: Series, st: Story, ch: Chapter, idx: int) -> str:
    total = len(st.chapters)
    prev_ch = st.chapters[idx - 1] if idx > 0 else None
    next_ch = st.chapters[idx + 1] if idx + 1 < total else None
    # Cross-story continuous reading.
    si = s.stories.index(st)
    prev_url = f"{prev_ch.slug}.html" if prev_ch else ""
    next_url = f"{next_ch.slug}.html" if next_ch else ""
    prev_label = f"{prev_ch.label}" + (f" \u2014 {prev_ch.subtitle}" if prev_ch and prev_ch.subtitle else "") if prev_ch else ""
    next_label = f"{next_ch.label}" + (f" \u2014 {next_ch.subtitle}" if next_ch and next_ch.subtitle else "") if next_ch else ""
    if not prev_ch and si > 0:
        ps = s.stories[si - 1]
        if ps.chapters:
            prev_url = f"../{ps.slug}/{ps.chapters[-1].slug}.html"
            prev_label = f"{ps.title} \u2014 {ps.chapters[-1].label}"
    if not next_ch and si + 1 < len(s.stories):
        ns = s.stories[si + 1]
        if ns.chapters:
            next_url = f"../{ns.slug}/{ns.chapters[0].slug}.html"
            next_label = f"{ns.title} \u2014 {ns.chapters[0].label}"

    toc_items = []
    for j, c in enumerate(st.chapters):
        cur = ' aria-current="true"' if j == idx else ""
        sub = f" \u2014 {html.escape(c.subtitle)}" if c.subtitle else ""
        toc_items.append(f'<li><a href="{c.slug}.html"{cur}><span>{j+1:02d}</span> {html.escape(c.label)}{sub}</a></li>')

    head_sub = f'<p class="ch-subtitle">{html.escape(ch.subtitle)}</p>' if ch.subtitle else ""
    # Chapter heading: keep it literary, not shouty.
    ch_title = ch.label if ch.slug != "opening" else "Opening"
    body_html = ch.__dict__.get("body_html", "")
    body = f"""
<div class="wrap reading-wrap">
  {breadcrumbs([("Library", "../../index.html"), (s.title, "../index.html"), (st.title, "index.html"), (ch.label, "")])}
  <div class="reading-grid">
    <aside class="toc" aria-label="Chapters in {html.escape(st.title)}">
      <details class="toc-box" open>
        <summary><span>{html.escape(st.title)}</span><small>{idx+1} / {total}</small></summary>
        <ol>{"".join(toc_items)}</ol>
      </details>
    </aside>
    <article class="reading" aria-labelledby="chTitle">
      <header class="ch-head">
        <p class="kicker">{html.escape(st.title)} &middot; {idx+1} of {total} &middot; {fmt_words(ch.words)} words</p>
        <h1 id="chTitle">{html.escape(ch_title)}</h1>
        {head_sub}
        <p class="ch-meta">~{reading_minutes(ch.words)} min &middot; <button class="linklike" id="tocToggle" type="button">Contents</button></p>
      </header>
      <div class="story-body" id="storyBody">
{body_html}
      </div>
      <footer class="ch-foot">
        <div class="ch-nav">
          {'<a class="prev" href="'+prev_url+'"><small>&larr; Previous</small><strong>'+html.escape(prev_label)+'</strong></a>' if prev_url else '<span></span>'}
          {'<a class="next" href="'+next_url+'"><small>Next &rarr;</small><strong>'+html.escape(next_label)+'</strong></a>' if next_url else '<span></span>'}
        </div>
        <p class="back"><a href="index.html">All chapters in {html.escape(st.title)}</a></p>
      </footer>
    </article>
  </div>
</div>"""
    return shell(title=f"{ch.label} \u2014 {st.title}", asset_prefix="../../", body=body,
                 body_class="page-chapter", prev_url=prev_url, next_url=next_url)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build() -> int:
    library = load_library()
    if SITE_ROOT.exists():
        shutil.rmtree(SITE_ROOT)
    SITE_ROOT.mkdir(parents=True, exist_ok=True)
    (SITE_ROOT / ".nojekyll").write_text("", encoding="utf-8")

    # Assets.
    dest_assets = SITE_ROOT / "assets"
    if ASSETS_SRC.exists():
        shutil.copytree(ASSETS_SRC, dest_assets)
    else:
        dest_assets.mkdir(parents=True, exist_ok=True)

    search_entries: list[dict] = []
    for s in library:
        write(SITE_ROOT / s.id / "index.html", series_page(s))
        for st in s.stories:
            si = s.stories.index(st)
            prev_story = s.stories[si - 1] if si > 0 else None
            next_story = s.stories[si + 1] if si + 1 < len(s.stories) else None
            write(SITE_ROOT / s.id / st.slug / "index.html", story_page(s, st, prev_story, next_story))
            for j, ch in enumerate(st.chapters):
                write(SITE_ROOT / s.id / st.slug / f"{ch.slug}.html",
                      chapter_page(s, st, ch, j))
                search_entries.append({
                    "series": s.title,
                    "story": st.title,
                    "chapter": ch.subtitle or ch.label,
                    "label": ch.label,
                    "url": f"{s.id}/{st.slug}/{ch.slug}.html",
                    "words": ch.words,
                    "excerpt": excerpt(ch.paras),
                })

    write(SITE_ROOT / "index.html", home_page(library))
    write(SITE_ROOT / "search.json", json.dumps(search_entries, ensure_ascii=False, indent=2))

    total_ch = sum(len(st.chapters) for s in library for st in s.stories)
    total_w = sum(s.words for s in library)
    print(f"Built {len(library)} series, {sum(len(s.stories) for s in library)} stories, "
          f"{total_ch} chapters, {total_w} words -> {SITE_ROOT}")
    for s in library:
        for st in s.stories:
            print(f"  {s.title} / {st.title}: " + ", ".join(
                f"{c.label}" + (f" ({c.subtitle})" if c.subtitle else "") for c in st.chapters))
    return 0


if __name__ == "__main__":
    sys.exit(build())
