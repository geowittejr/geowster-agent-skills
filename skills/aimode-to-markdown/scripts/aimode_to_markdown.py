#!/usr/bin/env python3
"""Convert a Google AI Mode conversation export into a Markdown transcript.

Input is the JSON produced by scripts/extract_aimode.js, either:
  * the plain .json file the script downloads, or
  * the gzip+base64url text assembled from window.__aimode.b64 chunks
    (any file whose content does not start with "{"; whitespace/newlines between
    chunks are ignored).

Usage:
  python3 aimode_to_markdown.py INPUT [-o OUTPUT.md] [--share-url URL]
                                [--no-heading-heuristic] [--keep-emoji/--no-emoji]

Standard library only (Python 3.8+).
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import gzip
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# Tiny DOM built from the sanitized HTML
# --------------------------------------------------------------------------- #
VOID = {"br", "hr", "img", "input", "meta", "link", "wbr", "source", "col", "area", "base", "embed", "track"}
BLOCK = {
    "div", "p", "section", "article", "header", "footer", "aside", "main", "nav",
    "ul", "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "figure", "figcaption",
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict | None = None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list = []
        self.parent = parent

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def has_block(self) -> bool:
        for c in self.children:
            if isinstance(c, Node) and (c.tag in BLOCK or c.attrs.get("role") == "heading" or c.has_block()):
                return True
        return False


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, dict(attrs), self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(html: str) -> Node:
    b = _TreeBuilder()
    b.feed(html)
    b.close()
    return b.root


# --------------------------------------------------------------------------- #
# HTML -> Markdown
# --------------------------------------------------------------------------- #
_WS = re.compile(r"[ \t\r\n\f\v ]+")


def _wrap(inner: str, mark: str) -> str:
    stripped = inner.strip()
    if not stripped:
        return inner
    lead = " " if inner[:1].isspace() else ""
    trail = " " if inner[-1:].isspace() else ""
    return f"{lead}{mark}{stripped}{mark}{trail}"


def inline(node: Node) -> str:
    out = []
    for c in node.children:
        if isinstance(c, str):
            out.append(_WS.sub(" ", c))
            continue
        t = c.tag
        if t in ("strong", "b"):
            out.append(_wrap(inline(c), "**"))
        elif t in ("em", "i"):
            out.append(_wrap(inline(c), "*"))
        elif t == "code":
            code = c.text()
            tick = "``" if "`" in code else "`"
            out.append(f"{tick}{code}{tick}" if code.strip() else "")
        elif t == "br":
            out.append("\n")
        else:
            out.append(inline(c))
    return "".join(out)


def _clean_line(s: str) -> str:
    s = _WS.sub(" ", s).strip()
    s = re.sub(r"\s+([.,;:!?)])", r"\1", s)  # "word ." -> "word."
    return s


def _heading_level(node: Node) -> int | None:
    if re.fullmatch(r"h[1-6]", node.tag):
        return int(node.tag[1])
    if node.attrs.get("role") == "heading":
        try:
            return int(node.attrs.get("aria-level", "3"))
        except ValueError:
            return 3
    return None


def _min_heading_level(node: Node) -> int | None:
    best = None
    stack = [node]
    while stack:
        n = stack.pop()
        if not isinstance(n, Node):
            continue
        lvl = _heading_level(n)
        if lvl is not None:
            best = lvl if best is None else min(best, lvl)
        stack.extend(n.children)
    return best


class Renderer:
    def __init__(self, heading_heuristic: bool = True, top_level: int | None = None):
        self.heading_heuristic = heading_heuristic
        # Shift headings so the reply's biggest heading renders as ### (speaker
        # labels are ##).
        self.offset = 3 - top_level if top_level is not None else 0

    # Paragraph that looks like a section title ("1. Runtimes & Package Managers")
    def _looks_like_heading(self, node: Node, text: str) -> bool:
        if not self.heading_heuristic or not text or len(text) > 90:
            return False
        if text.endswith((".", ":", "?", "!", ",", ";")):
            return False
        if re.match(r"^\d+\.\s+\S", text):
            return True
        # Paragraph whose only content is one bold run
        kids = [c for c in node.children if not (isinstance(c, str) and not c.strip())]
        return len(kids) == 1 and isinstance(kids[0], Node) and kids[0].tag in ("strong", "b")

    def blocks(self, node: Node, indent: str = "") -> str:
        out: list[str] = []
        buf: list = []

        def flush():
            if not buf:
                return
            tmp = Node("span")
            tmp.children = list(buf)
            text = _clean_line(inline(tmp))
            buf.clear()
            if text:
                out.append(f"{indent}{text}\n\n")

        for c in node.children:
            if isinstance(c, str) or (c.tag not in BLOCK and c.attrs.get("role") != "heading" and not c.has_block()):
                buf.append(c)
                continue
            flush()
            out.append(self.block(c, indent))
        flush()
        return "".join(out)

    def block(self, c: Node, indent: str) -> str:
        level = _heading_level(c)
        if level is not None:
            text = _clean_line(inline(c))
            return f"\n{'#' * min(6, max(3, level + self.offset))} {text}\n\n" if text else ""
        if c.tag in ("ul", "ol"):
            return self.list_(c, indent)
        if c.tag == "table":
            return self.table(c)
        if c.tag == "pre":
            code = c.text().rstrip("\n")
            return f"```\n{code}\n```\n\n"
        if c.tag == "blockquote":
            inner = self.blocks(c).strip()
            return "\n".join("> " + l if l else ">" for l in inner.splitlines()) + "\n\n"
        if c.tag == "hr":
            return "---\n\n"
        # Generic container: a leaf block becomes a paragraph (maybe a heading)
        if not c.has_block():
            text = _clean_line(inline(c))
            if not text:
                return ""
            if not indent and self._looks_like_heading(c, text):
                return f"\n### {text.strip('*').strip()}\n\n"
            return f"{indent}{text}\n\n"
        return self.blocks(c, indent)

    def list_(self, lst: Node, indent: str) -> str:
        out = []
        n = 1
        for li in lst.children:
            if not isinstance(li, Node) or li.tag != "li":
                continue
            nested = []
            body = Node("li")
            for ch in li.children:
                if isinstance(ch, Node) and ch.tag in ("ul", "ol"):
                    nested.append(ch)
                else:
                    body.children.append(ch)
            text = self.blocks(body).strip()
            text = re.sub(r"\n\s*\n", " ", text)  # keep each item on one logical line
            marker = f"{n}. " if lst.tag == "ol" else "- "
            n += 1
            out.append(f"{indent}{marker}{text}\n")
            for sub in nested:
                out.append(self.list_(sub, indent + "  ").rstrip("\n") + "\n")
        return "".join(out) + "\n"

    def table(self, tbl: Node) -> str:
        rows = []

        def walk(n: Node):
            for ch in n.children:
                if isinstance(ch, Node):
                    if ch.tag == "tr":
                        rows.append([
                            _clean_line(inline(td)).replace("|", "\\|")
                            for td in ch.children if isinstance(td, Node) and td.tag in ("td", "th")
                        ])
                    else:
                        walk(ch)

        walk(tbl)
        rows = [r for r in rows if r]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join(["---"] * width) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines) + "\n\n"


# Google sometimes renders a list item's lead term as a link followed by a bold
# colon ("<a>Git</a><strong>:</strong>"), which would become "Git**:**".
# Pull the lead term into the bold run: "**Git:**".
_SPLIT_BOLD = re.compile(r"^(\s*(?:[-*]|\d+\.)\s+)([^*\n]{1,60}?)\s*\*\*([:&][^*\n]*)\*\*", re.M)


def html_to_markdown(html: str, heading_heuristic: bool = True) -> str:
    root = parse_html(html)
    md = Renderer(heading_heuristic, _min_heading_level(root)).blocks(root)
    md = _SPLIT_BOLD.sub(lambda m: f"{m.group(1)}**{m.group(2).strip()} {m.group(3).strip()}**"
                         if m.group(3).startswith("&") else f"{m.group(1)}**{m.group(2).strip()}{m.group(3).strip()}**", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _real_url(href: str) -> str:
    """Unwrap google.com/url?q=... redirects."""
    try:
        p = urlparse(href)
        if "google." in p.netloc and p.path == "/url":
            qs = parse_qs(p.query)
            for k in ("q", "url"):
                if qs.get(k):
                    return qs[k][0]
    except Exception:
        pass
    return href


def render_sources(sources: list) -> str:
    """Card = aria-label title + text lines like [site, (·, author), title, snippet]."""
    items = []
    for s in sources or []:
        lines = [
            l.strip() for l in s.get("lines", [])
            if l and l.strip() not in ("·", "Show all")
            and not re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?|\d+\s?[smh]", l.strip())  # video durations
        ]
        title = re.sub(r",\s*Video,\s*watch on YouTube\s*$", "", (s.get("title") or "").strip())
        if not title and not lines:
            continue
        if title and title in lines:
            # Last occurrence: YouTube cards read "YouTube · Author · YouTube · snippet"
            i = len(lines) - 1 - lines[::-1].index(title)
            site_parts, snippet_parts = lines[:i], lines[i + 1:]
        elif title:
            # Title may be a prefix of a longer line (truncated label) or absent.
            i = next((k for k, l in enumerate(lines) if l.startswith(title) or title.startswith(l)), None)
            site_parts = lines[:i] if i is not None else lines[:1]
            snippet_parts = lines[i + 1:] if i is not None else lines[1:]
        else:
            title = lines[1] if len(lines) > 1 else lines[0]
            site_parts, snippet_parts = lines[:1], lines[2:]
        site = " · ".join(dict.fromkeys(site_parts)) or "Source"
        snippet = " ".join(p for p in snippet_parts if p != title).strip()
        # A snippet often repeats the full title as its first words; trim that.
        if title and snippet.startswith(title + " "):
            snippet = snippet[len(title):].strip()
        url = _real_url(s.get("href", ""))
        title_md = title.replace("[", "\\[").replace("]", "\\]")
        entry = f"- {site} — [{title_md}]({url})" if url else f"- {site} — {title_md}"
        if snippet:
            entry += f" — {snippet[:200]}{'…' if len(snippet) > 200 else ''}"
        items.append(entry)
    return ("**Sources:**\n" + "\n".join(items) + "\n") if items else ""


# --------------------------------------------------------------------------- #
# Input decoding & document assembly
# --------------------------------------------------------------------------- #
def load_export(path: str) -> dict:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    s = raw.strip()
    if s.startswith("{"):
        return json.loads(s)
    s = re.sub(r"\s+", "", s)
    s += "=" * (-len(s) % 4)
    data = base64.urlsafe_b64decode(s.replace("+", "-").replace("/", "_"))
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8"))


def build_markdown(export: dict, share_url: str | None, heading_heuristic: bool, emoji: bool) -> str:
    title = export.get("title") or "AI Mode conversation"
    captured = export.get("captured") or _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        captured_h = _dt.datetime.fromisoformat(captured.replace("Z", "+00:00")).strftime("%B %d, %Y").replace(" 0", " ")
    except ValueError:
        captured_h = captured
    you = "🧑 You" if emoji else "You"
    bot = "🤖 AI Mode" if emoji else "AI Mode"

    parts = [f"# AI Mode Conversation: {title}\n"]
    src = share_url or export.get("page_url")
    meta = f"*Source: Google AI Mode shared conversation{' — ' + src if src else ''}*  \n*Captured: {captured_h}*\n"
    parts.append(meta)
    parts.append(
        "> Note: Inline citation chips (e.g. \"GitHub +2\") are removed from the body text; "
        "source cards shown under a reply are listed under **Sources**.\n"
    )
    mismatch = export.get("turn_count_mismatch")
    if mismatch:
        parts.append(f"> ⚠️ Turn count mismatch on page: {mismatch}. Some turns may be missing.\n")

    for t in export.get("turns", []):
        parts.append("---\n")
        when = f" — {t['time']}" if t.get("time") else ""
        parts.append(f"## {you}{when}\n\n**{t.get('question', '').strip()}**\n")
        parts.append(f"## {bot}\n\n{html_to_markdown(t.get('reply_html', ''), heading_heuristic)}\n")
        s = render_sources(t.get("sources"))
        if s:
            parts.append(s)
    return "\n".join(parts).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="aimode-*.json, a base64 chunk file, or '-' for stdin")
    ap.add_argument("-o", "--output", help="Output .md path (default: derived from the title)")
    ap.add_argument("--share-url", help="Original share.google link to record in the header")
    ap.add_argument("--no-heading-heuristic", action="store_true",
                    help="Don't promote short '1. Title' / bold-only paragraphs to ### headings")
    ap.add_argument("--no-emoji", action="store_true", help="Plain 'You' / 'AI Mode' speaker labels")
    args = ap.parse_args(argv)

    export = load_export(args.input)
    md = build_markdown(export, args.share_url, not args.no_heading_heuristic, not args.no_emoji)

    if args.output:
        out = Path(args.output)
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", (export.get("title") or "aimode").lower()).strip("-")[:60]
        out = Path(f"aimode-{slug or 'conversation'}.md")
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out} ({len(export.get('turns', []))} turns, {len(md):,} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
