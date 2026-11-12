"""Allow-list HTML sanitizer for agent output rendered from Markdown (agents may echo collected web text)."""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser

ALLOWED = {"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "b", "i", "code", "pre",
           "blockquote", "table", "thead", "tbody", "tr", "th", "td", "a", "del", "sup", "sub", "span", "div"}
VOID = {"br", "hr"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math"}


class _San(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED:
            return
        keep = ""
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://", "#")):
                keep = f' href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
        elif tag in ("th", "td"):
            al = dict(attrs).get("align") or ""
            if al in ("left", "right", "center"):
                keep = f' style="text-align:{al}"'
        self.out.append(f"<{tag}{keep}>")

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if not self.skip and tag in ALLOWED and tag not in VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(escape(data, quote=False))


def sanitize(html: str) -> str:
    s = _San()
    s.feed(html or "")
    s.close()
    return "".join(s.out)
