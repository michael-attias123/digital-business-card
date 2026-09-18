#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated conformance suite for the digital business card.

Verifies the project against every requirement in the assignment brief plus a
layer of quality checks (accessibility, WCAG contrast, reference integrity,
dead CSS).  Pure standard library - no packages to install, and deliberately
written in Python so that the repository contains no JavaScript at all.

    python tests/check.py            # run the suite
    python tests/check.py --verbose  # also list every passing check
    python tests/check.py --w3c      # additionally POST the HTML to the W3C
                                     # Nu validator (requires a network call)

Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_FILE = ROOT / "index.html"
CSS_FILE = ROOT / "css" / "style.css"

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}

# Modifier classes that exist in the CSS on purpose even though the current
# content does not use them (the meter scale is 1-5; today nobody is rated 1-2).
UNUSED_CSS_ALLOWLIST = {"meter--1", "meter--2"}


# ---------------------------------------------------------------------------
# A very small DOM
# ---------------------------------------------------------------------------

class Node:
    def __init__(self, tag, attrs=None, parent=None, line=0):
        self.tag = tag
        self.attrs = attrs or {}
        self.parent = parent
        self.children = []      # Node | str
        self.line = line

    # -- traversal ---------------------------------------------------------
    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    # -- content -----------------------------------------------------------
    @property
    def text(self):
        parts = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text)
        return "".join(parts)

    @property
    def classes(self):
        return set((self.attrs.get("class") or "").split())

    def __repr__(self):
        return f"<{self.tag} line {self.line}>"


class DomBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]
        self.structure_errors = []

    def _open(self, tag, attrs):
        node = Node(tag, dict(attrs), self.stack[-1], self.getpos()[0])
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._open(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                for orphan in self.stack[index + 1:]:
                    self.structure_errors.append(
                        f"<{orphan.tag}> opened on line {orphan.line} is never closed"
                    )
                del self.stack[index:]
                return
        self.structure_errors.append(
            f"stray </{tag}> on line {self.getpos()[0]}"
        )

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def close(self):
        super().close()
        for orphan in self.stack[1:]:
            self.structure_errors.append(
                f"<{orphan.tag}> opened on line {orphan.line} is never closed"
            )


# ---------------------------------------------------------------------------
# CSS helpers
# ---------------------------------------------------------------------------

def strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.S)


def css_selectors(css: str):
    """Every selector / at-rule prelude in the sheet, nested ones included."""
    out, buf = [], []
    for char in css:
        if char == "{":
            selector = "".join(buf).strip()
            if selector:
                out.append(selector)
            buf = []
        elif char in "};":
            buf = []
        else:
            buf.append(char)
    return out


def css_block(css: str, selector: str):
    """The declaration body of the rule whose selector is exactly `selector`."""
    match = re.search(re.escape(selector) + r"\s*\{", css)
    if not match:
        return None
    brace = match.end() - 1
    depth, index = 1, brace + 1
    while index < len(css) and depth:
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
        index += 1
    return css[brace + 1:index - 1]


def custom_props(block: str) -> dict:
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block or "")
    }


# ---------------------------------------------------------------------------
# WCAG contrast
# ---------------------------------------------------------------------------

def parse_hex(value: str):
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        return None
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def relative_luminance(rgb):
    channels = []
    for raw in rgb:
        c = raw / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg_hex, bg_hex):
    fg, bg = parse_hex(fg_hex), parse_hex(bg_hex)
    if not fg or not bg:
        return None
    a, b = relative_luminance(fg), relative_luminance(bg)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

class Fail(Exception):
    pass


class Warn(Exception):
    pass


CHECKS = []


def check(category, name):
    def decorator(fn):
        CHECKS.append((category, name, fn))
        return fn
    return decorator


class Context:
    def __init__(self):
        if not HTML_FILE.exists():
            sys.exit("index.html not found - run this from the project root.")
        if not CSS_FILE.exists():
            sys.exit("css/style.css not found.")

        self.html = HTML_FILE.read_text(encoding="utf-8")
        self.css_raw = CSS_FILE.read_text(encoding="utf-8")
        self.css = strip_css_comments(self.css_raw)

        builder = DomBuilder()
        builder.feed(self.html)
        builder.close()
        self.dom = builder.root
        self.structure_errors = builder.structure_errors

        self.nodes = list(self.dom.walk())
        self.ids = [n.attrs["id"] for n in self.nodes if n.attrs.get("id")]
        self.selectors = css_selectors(self.css)

    # -- lookups -----------------------------------------------------------
    def tags(self, *names):
        wanted = set(names)
        return [n for n in self.nodes if n.tag in wanted]

    def by_class(self, name):
        return [n for n in self.nodes if name in n.classes]

    def by_id(self, name):
        for n in self.nodes:
            if n.attrs.get("id") == name:
                return n
        return None

    def first(self, cls):
        found = self.by_class(cls)
        if not found:
            raise Fail(f"no element with class .{cls} was found")
        return found[0]

    def refs(self, attr):
        return [(n, n.attrs[attr]) for n in self.nodes if n.attrs.get(attr)]

    def all_hrefs(self):
        return [v for _, v in self.refs("href")] + [v for _, v in self.refs("src")]


# ===========================================================================
# 1. Assignment constraints: HTML + CSS only, external stylesheet
# ===========================================================================

CONSTRAINTS = "Assignment constraints"


@check(CONSTRAINTS, "No <script> element anywhere in the page")
def _(ctx):
    found = ctx.tags("script")
    if found:
        raise Fail(f"{len(found)} <script> element(s) found (line {found[0].line})")


@check(CONSTRAINTS, "No inline event handlers (onclick, onload, ...)")
def _(ctx):
    offenders = [
        (n, attr) for n in ctx.nodes for attr in n.attrs
        if attr.startswith("on") and attr not in {"one"}
    ]
    if offenders:
        node, attr = offenders[0]
        raise Fail(f'{attr}="..." on <{node.tag}> line {node.line}')


@check(CONSTRAINTS, "No javascript: URLs")
def _(ctx):
    bad = [v for v in ctx.all_hrefs() if v.strip().lower().startswith("javascript:")]
    if bad:
        raise Fail(f"found {bad[0]!r}")


@check(CONSTRAINTS, "No JavaScript source files shipped with the site")
def _(ctx):
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*")
        if p.is_file()
        and p.suffix.lower() in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}
        and ".git" not in p.parts
    ]
    if offenders:
        raise Fail("found " + ", ".join(offenders))


@check(CONSTRAINTS, "No <style> element inside the HTML")
def _(ctx):
    found = ctx.tags("style")
    if found:
        raise Fail(f"<style> on line {found[0].line}")


@check(CONSTRAINTS, 'No inline style="..." attributes')
def _(ctx):
    offenders = [n for n in ctx.nodes if "style" in n.attrs]
    if offenders:
        raise Fail(f"<{offenders[0].tag}> on line {offenders[0].line}")


@check(CONSTRAINTS, "Stylesheet is linked as an external file")
def _(ctx):
    links = [
        n for n in ctx.tags("link")
        if "stylesheet" in (n.attrs.get("rel") or "")
        and not n.attrs.get("href", "").startswith("http")
    ]
    if not links:
        raise Fail("no local <link rel=stylesheet> found")
    if links[0].attrs["href"] != "css/style.css":
        raise Fail(f"unexpected stylesheet path {links[0].attrs['href']!r}")


@check(CONSTRAINTS, "External stylesheet actually carries the design")
def _(ctx):
    rules = len(ctx.selectors)
    if rules < 60:
        raise Fail(f"only {rules} rules in style.css - is the design really external?")


# ===========================================================================
# 2. Required content (the brief's minimum requirements)
# ===========================================================================

CONTENT = "Required content"


@check(CONTENT, "Full name appears in the single <h1>")
def _(ctx):
    h1s = ctx.tags("h1")
    if len(h1s) != 1:
        raise Fail(f"expected exactly one <h1>, found {len(h1s)}")
    text = h1s[0].text
    for expected in ("מיכאל אטיאס", "Michael Attias"):
        if expected not in text:
            raise Fail(f"{expected!r} missing from the <h1>")


@check(CONTENT, "Role / field of specialisation is stated")
def _(ctx):
    role = ctx.first("hero__role")
    he = [n for n in role.walk() if "he" in n.classes]
    en = [n for n in role.walk() if "en" in n.classes]
    if not he or not en:
        raise Fail("role is missing one of its two languages")
    if len(he[0].text.strip()) < 8 or len(en[0].text.strip()) < 8:
        raise Fail("role text looks empty")


@check(CONTENT, "Profile picture is present, local and has alt text")
def _(ctx):
    images = ctx.tags("img")
    if not images:
        raise Fail("no <img> element on the page")
    img = images[0]
    src = img.attrs.get("src", "")
    if not src:
        raise Fail("<img> has no src")
    if not (ROOT / src).exists():
        raise Fail(f"image file {src!r} does not exist")
    if not img.attrs.get("alt", "").strip():
        raise Fail("profile image has no alt text")


@check(CONTENT, "About paragraph covers background, interests and projects")
def _(ctx):
    about = ctx.first("about__text")
    he = [n for n in about.walk() if "he" in n.classes]
    en = [n for n in about.walk() if "en" in n.classes]
    if not he or not en:
        raise Fail("the about paragraph is missing a language")
    if len(he[0].text.strip()) < 250:
        raise Fail(f"Hebrew paragraph is only {len(he[0].text.strip())} characters")
    if len(en[0].text.strip()) < 250:
        raise Fail(f"English paragraph is only {len(en[0].text.strip())} characters")


@check(CONTENT, "Link to a GitHub account")
def _(ctx):
    hits = [v for v in ctx.all_hrefs() if re.match(r"https://github\.com/[\w-]+", v)]
    if not hits:
        raise Fail("no https://github.com/<user> link found")


@check(CONTENT, "Link to a LinkedIn profile")
def _(ctx):
    hits = [v for v in ctx.all_hrefs()
            if re.match(r"https://(www\.)?linkedin\.com/in/[\w-]+", v)]
    if not hits:
        raise Fail("no https://linkedin.com/in/<profile> link found")


@check(CONTENT, "Phone number offered as a tel: link")
def _(ctx):
    hits = [v for v in ctx.all_hrefs() if v.startswith("tel:")]
    if not hits:
        raise Fail("no tel: link found")
    if not re.fullmatch(r"tel:\+?[\d\-\s]{7,}", hits[0]):
        raise Fail(f"{hits[0]!r} does not look like a phone number")


@check(CONTENT, "Email address offered as a mailto: link")
def _(ctx):
    hits = [v for v in ctx.all_hrefs() if v.startswith("mailto:")]
    if not hits:
        raise Fail("no mailto: link found")
    if not re.fullmatch(r"mailto:[^@\s]+@[^@\s]+\.[a-z]{2,}", hits[0]):
        raise Fail(f"{hits[0]!r} is not a valid address")


@check(CONTENT, "Extra sections beyond the minimum (skills, projects, journey)")
def _(ctx):
    for section_id in ("skills", "projects", "timeline"):
        if not ctx.by_id(section_id):
            raise Fail(f"section #{section_id} is missing")


# ===========================================================================
# 3. Light / dark mode, implemented without JavaScript
# ===========================================================================

THEME = "Dark / light mode"


@check(THEME, "A checkbox drives the theme")
def _(ctx):
    node = ctx.by_id("theme-switch")
    if node is None:
        raise Fail("#theme-switch does not exist")
    if node.tag != "input" or node.attrs.get("type") != "checkbox":
        raise Fail("#theme-switch is not a checkbox input")


@check(THEME, "A visible <label> operates that checkbox")
def _(ctx):
    labels = [n for n in ctx.tags("label") if n.attrs.get("for") == "theme-switch"]
    if not labels:
        raise Fail('no <label for="theme-switch">')


@check(THEME, "The switch is reachable and describable by assistive tech")
def _(ctx):
    labels = [n for n in ctx.tags("label") if n.attrs.get("for") == "theme-switch"]
    named = any("sr-only" in n.classes for label in labels for n in label.walk())
    if not named:
        raise Fail("the theme label carries no text for screen readers")


@check(THEME, "CSS reacts to :checked to repaint the page")
def _(ctx):
    if "#theme-switch:checked ~ .page" not in ctx.css:
        raise Fail("no '#theme-switch:checked ~ .page' rule in the stylesheet")


@check(THEME, "A full second palette is defined for dark mode")
def _(ctx):
    light = custom_props(css_block(ctx.css, ".page"))
    dark = custom_props(css_block(ctx.css, "#theme-switch:checked ~ .page"))
    if not dark:
        raise Fail("the dark block defines no custom properties")
    colourish = {k for k in light if any(
        k.startswith(p) for p in ("--bg", "--fg", "--brand", "--line", "--glow", "--ok")
    )}
    missing = sorted(colourish - set(dark))
    if missing:
        raise Fail("not overridden in dark mode: " + ", ".join(missing))


@check(THEME, "Both themes are declared to the browser via color-scheme")
def _(ctx):
    if "color-scheme" not in ctx.css:
        raise Fail("color-scheme is never declared")


# ===========================================================================
# 4. Responsiveness
# ===========================================================================

RESPONSIVE = "Responsiveness"


@check(RESPONSIVE, "Viewport meta tag is present")
def _(ctx):
    metas = [n for n in ctx.tags("meta") if n.attrs.get("name") == "viewport"]
    if not metas:
        raise Fail("no <meta name=viewport>")
    content = metas[0].attrs.get("content", "")
    if "width=device-width" not in content:
        raise Fail(f"viewport content is {content!r}")


@check(RESPONSIVE, "Viewport does not block pinch-zoom")
def _(ctx):
    metas = [n for n in ctx.tags("meta") if n.attrs.get("name") == "viewport"]
    content = metas[0].attrs.get("content", "") if metas else ""
    if "user-scalable=no" in content or "maximum-scale=1" in content:
        raise Fail("zooming is disabled, which fails WCAG 1.4.4")


@check(RESPONSIVE, "Media queries adapt the layout to several screen sizes")
def _(ctx):
    queries = [s for s in ctx.selectors if s.startswith("@media")]
    widths = [s for s in queries if "width" in s]
    if len(widths) < 3:
        raise Fail(f"only {len(widths)} width-based media queries")


@check(RESPONSIVE, "No fixed width wider than a phone screen")
def _(ctx):
    body = re.sub(r"@media[^{]*\{", "{", ctx.css)
    offenders = [
        (prop, int(value))
        for prop, value in re.findall(r"(?<![\w-])((?:min-)?(?:inline-size|width))\s*:\s*(\d+)px", body)
        if int(value) > 420
    ]
    if offenders:
        prop, value = offenders[0]
        raise Fail(f"{prop}: {value}px would overflow a small phone")


@check(RESPONSIVE, "Layout uses fluid units, not a fixed pixel grid")
def _(ctx):
    for token in ("clamp(", "minmax(", "auto-fit", "%"):
        if token not in ctx.css:
            raise Fail(f"{token!r} never appears in the stylesheet")


@check(RESPONSIVE, "Logical properties keep the layout correct in RTL and LTR")
def _(ctx):
    for prop in ("inline-size", "inset-inline-start", "padding-inline", "margin-inline"):
        if prop not in ctx.css:
            raise Fail(f"{prop} is never used")


# ===========================================================================
# 5. HTML validity and semantics
# ===========================================================================

HTML_OK = "HTML structure"


@check(HTML_OK, "Document starts with an HTML5 doctype")
def _(ctx):
    if not ctx.html.lstrip().lower().startswith("<!doctype html>"):
        raise Fail("missing or non-HTML5 doctype")


@check(HTML_OK, "<html> declares a language and a direction")
def _(ctx):
    html = ctx.tags("html")
    if not html:
        raise Fail("no <html> element")
    if not html[0].attrs.get("lang"):
        raise Fail("lang attribute missing")
    if not html[0].attrs.get("dir"):
        raise Fail("dir attribute missing")


@check(HTML_OK, "Character encoding is declared")
def _(ctx):
    if not [n for n in ctx.tags("meta") if "charset" in n.attrs]:
        raise Fail("no <meta charset>")


@check(HTML_OK, "Page has a descriptive <title> and meta description")
def _(ctx):
    titles = ctx.tags("title")
    page_titles = [t for t in titles if t.parent and t.parent.tag == "head"]
    if not page_titles or len(page_titles[0].text.strip()) < 10:
        raise Fail("title is missing or too short")
    if not [n for n in ctx.tags("meta") if n.attrs.get("name") == "description"]:
        raise Fail("no meta description")


@check(HTML_OK, "Every element is properly closed and nested")
def _(ctx):
    if ctx.structure_errors:
        raise Fail(ctx.structure_errors[0])


@check(HTML_OK, "Exactly one <main> landmark")
def _(ctx):
    mains = ctx.tags("main")
    if len(mains) != 1:
        raise Fail(f"found {len(mains)} <main> elements")


@check(HTML_OK, "Semantic landmarks are used instead of bare divs")
def _(ctx):
    for tag in ("header", "nav", "main", "section", "article", "footer", "address"):
        if not ctx.tags(tag):
            raise Fail(f"<{tag}> is never used")


@check(HTML_OK, "Heading levels descend without skipping")
def _(ctx):
    levels = [int(n.tag[1]) for n in ctx.nodes if re.fullmatch(r"h[1-6]", n.tag)]
    if not levels:
        raise Fail("no headings at all")
    if levels[0] != 1:
        raise Fail(f"the first heading is an h{levels[0]}, not an h1")
    for previous, current in zip(levels, levels[1:]):
        if current > previous + 1:
            raise Fail(f"jump from h{previous} to h{current}")


@check(HTML_OK, "All id attributes are unique")
def _(ctx):
    seen, dupes = set(), set()
    for value in ctx.ids:
        (dupes if value in seen else seen).add(value)
    if dupes:
        raise Fail("duplicated: " + ", ".join(sorted(dupes)))


@check(HTML_OK, "Every label[for] points at an element that exists")
def _(ctx):
    missing = sorted({
        value for node, value in ctx.refs("for") if value not in ctx.ids
    })
    if missing:
        raise Fail("dangling: " + ", ".join(missing))


@check(HTML_OK, "Every #fragment link resolves (nav, skip link, SVG icons)")
def _(ctx):
    missing = sorted({
        value[1:] for value in ctx.all_hrefs()
        if value.startswith("#") and len(value) > 1 and value[1:] not in ctx.ids
    })
    if missing:
        raise Fail("broken anchors: " + ", ".join(missing))


@check(HTML_OK, "Every aria-labelledby points at an element that exists")
def _(ctx):
    missing = sorted({
        token
        for _, value in ctx.refs("aria-labelledby")
        for token in value.split()
        if token not in ctx.ids
    })
    if missing:
        raise Fail("dangling: " + ", ".join(missing))


@check(HTML_OK, "Every local file referenced by the page exists on disk")
def _(ctx):
    missing = sorted({
        value for value in ctx.all_hrefs()
        if not value.startswith(("http:", "https:", "mailto:", "tel:", "#", "data:"))
        and not (ROOT / value).exists()
    })
    if missing:
        raise Fail("missing: " + ", ".join(missing))


@check(HTML_OK, "No empty or placeholder links")
def _(ctx):
    bad = [
        n for n in ctx.tags("a")
        if n.attrs.get("href", "").strip() in ("", "#")
    ]
    if bad:
        raise Fail(f"<a> on line {bad[0].line} has a placeholder href")


# ===========================================================================
# 6. Accessibility
# ===========================================================================

A11Y = "Accessibility"


@check(A11Y, "A skip link comes before the content")
def _(ctx):
    links = ctx.by_class("skip-link")
    if not links:
        raise Fail("no .skip-link element")
    if links[0].attrs.get("href") != "#main":
        raise Fail("the skip link does not point at #main")


@check(A11Y, "Every <section> has an accessible name")
def _(ctx):
    unnamed = [
        n for n in ctx.tags("section")
        if not (n.attrs.get("aria-labelledby") or n.attrs.get("aria-label"))
    ]
    if unnamed:
        raise Fail(f"<section> on line {unnamed[0].line} is unnamed")


@check(A11Y, 'Links with target="_blank" are protected with rel=noopener')
def _(ctx):
    bad = [
        n for n in ctx.tags("a")
        if n.attrs.get("target") == "_blank"
        and "noopener" not in (n.attrs.get("rel") or "")
    ]
    if bad:
        raise Fail(f"<a href={bad[0].attrs.get('href')!r}> is missing rel=noopener")


@check(A11Y, "Decorative icons are hidden from screen readers")
def _(ctx):
    exposed = []
    for svg in ctx.tags("svg"):
        if "icon" not in svg.classes:
            continue
        chain = [svg, *svg.ancestors()]
        if not any(n.attrs.get("aria-hidden") == "true" for n in chain):
            exposed.append(svg)
    if exposed:
        raise Fail(f"<svg> on line {exposed[0].line} is announced to screen readers")


@check(A11Y, "Keyboard focus is always visible")
def _(ctx):
    if ":focus-visible" not in ctx.css:
        raise Fail("no :focus-visible styling in the stylesheet")


@check(A11Y, "Users who ask for less motion get a still page")
def _(ctx):
    blocks = [s for s in ctx.selectors
              if s.startswith("@media") and "prefers-reduced-motion: reduce" in s]
    if not blocks:
        raise Fail("no @media (prefers-reduced-motion: reduce) block")


@check(A11Y, "Light palette meets WCAG AA contrast")
def _(ctx):
    _assert_palette_contrast(ctx, dark=False)


@check(A11Y, "Dark palette meets WCAG AA contrast")
def _(ctx):
    _assert_palette_contrast(ctx, dark=True)


CONTRAST_PAIRS = [
    ("--fg", "--bg-elev", 7.0, "body text on a card"),
    ("--fg", "--bg", 7.0, "body text on the page"),
    ("--fg-muted", "--bg-elev", 4.5, "secondary text on a card"),
    ("--fg-muted", "--bg-sunk", 4.5, "secondary text on the alternate band"),
    ("--accent-fg", "--bg-elev", 4.5, "accent text on a card"),
    ("--accent-fg", "--bg-sunk", 4.5, "accent text on the alternate band"),
    ("--on-brand", "--btn-1", 4.5, "button label on the brand gradient (start)"),
    ("--on-brand", "--btn-2", 4.5, "button label on the brand gradient (end)"),
    ("--ok", "--bg-elev", 4.5, "availability badge"),
]


def _assert_palette_contrast(ctx, dark):
    palette = custom_props(css_block(ctx.css, ".page"))
    if dark:
        palette.update(custom_props(css_block(ctx.css, "#theme-switch:checked ~ .page")))

    problems = []
    for fg, bg, minimum, label in CONTRAST_PAIRS:
        ratio = contrast_ratio(palette.get(fg, ""), palette.get(bg, ""))
        if ratio is None:
            continue
        if ratio < minimum:
            problems.append(f"{label}: {ratio:.2f}:1 (needs {minimum}:1)")
    if problems:
        raise Fail("; ".join(problems))


# ===========================================================================
# 7. Bilingual machinery
# ===========================================================================

BILINGUAL = "Bilingual mode"


@check(BILINGUAL, "Two radio inputs drive the language")
def _(ctx):
    for node_id in ("lang-he", "lang-en"):
        node = ctx.by_id(node_id)
        if node is None or node.attrs.get("type") != "radio":
            raise Fail(f"#{node_id} is not a radio input")
    if "checked" not in ctx.by_id("lang-he").attrs:
        raise Fail("Hebrew is not the default language")
    if "checked" in ctx.by_id("lang-en").attrs:
        raise Fail("both languages are marked as checked")


@check(BILINGUAL, "Every Hebrew string has an English counterpart")
def _(ctx):
    he, en = len(ctx.by_class("he")), len(ctx.by_class("en"))
    if he != en:
        raise Fail(f"{he} Hebrew fragments vs {en} English fragments")
    if he < 40:
        raise Fail(f"only {he} translated fragments - the page cannot be fully bilingual")


@check(BILINGUAL, "English fragments are tagged lang=\"en\"")
def _(ctx):
    untagged = [n for n in ctx.by_class("en") if n.attrs.get("lang") != "en"]
    if untagged:
        raise Fail(f"<{untagged[0].tag}> on line {untagged[0].line} has no lang attribute "
                   f"({len(untagged)} in total)")


@check(BILINGUAL, "Switching language also switches writing direction")
def _(ctx):
    block = css_block(ctx.css, "#lang-en:checked ~ .page")
    if not block or "direction: ltr" not in block:
        raise Fail("no rule flips the page to direction: ltr")


@check(BILINGUAL, "Only one language is rendered at a time")
def _(ctx):
    if not re.search(r"\.en\s*\{[^}]*display:\s*none", ctx.css):
        raise Fail("English is not hidden by default")
    if "#lang-en:checked ~ .page .he" not in ctx.css:
        raise Fail("Hebrew is never hidden when English is selected")


# ===========================================================================
# 8. Stylesheet quality
# ===========================================================================

QUALITY = "Stylesheet quality"


@check(QUALITY, "Braces are balanced")
def _(ctx):
    opened, closed = ctx.css.count("{"), ctx.css.count("}")
    if opened != closed:
        raise Fail(f"{opened} '{{' vs {closed} '}}'")


@check(QUALITY, "Design tokens drive the theme")
def _(ctx):
    tokens = custom_props(css_block(ctx.css, ".page"))
    if len(tokens) < 20:
        raise Fail(f"only {len(tokens)} custom properties defined")


@check(QUALITY, "Every var() refers to a property that is defined")
def _(ctx):
    defined = set(re.findall(r"(--[\w-]+)\s*:", ctx.css))
    used = set(re.findall(r"var\(\s*(--[\w-]+)", ctx.css))
    missing = sorted(used - defined)
    if missing:
        raise Fail("undefined: " + ", ".join(missing))


@check(QUALITY, "Every class used in the HTML is styled")
def _(ctx):
    used = {c for n in ctx.nodes for c in n.classes}
    styled = {
        m for selector in ctx.selectors
        for m in re.findall(r"\.(-?[A-Za-z_][\w-]*)", selector)
    }
    orphans = sorted(used - styled)
    if orphans:
        # Almost always a typo in a class name, so this is a hard failure.
        raise Fail("classes with no CSS rule: " + ", ".join(orphans))


@check(QUALITY, "No dead CSS rules")
def _(ctx):
    used = {c for n in ctx.nodes for c in n.classes}
    styled = {
        m for selector in ctx.selectors
        for m in re.findall(r"\.(-?[A-Za-z_][\w-]*)", selector)
    }
    dead = sorted(styled - used - UNUSED_CSS_ALLOWLIST)
    if dead:
        raise Warn("selectors matching nothing: " + ", ".join(dead))


@check(QUALITY, "Web fonts degrade to a real fallback stack")
def _(ctx):
    block = css_block(ctx.css, ".page") or ""
    family = re.search(r"--font-sans:\s*([^;]+);", block)
    if not family:
        raise Fail("no --font-sans token")
    if family.group(1).count(",") < 3:
        raise Fail("the font stack has almost no fallbacks")
    if "sans-serif" not in family.group(1):
        raise Fail("the font stack does not end in a generic family")


@check(QUALITY, "A print stylesheet is provided")
def _(ctx):
    if not [s for s in ctx.selectors if s.startswith("@media print")]:
        raise Fail("no @media print block")


@check(QUALITY, "Progressive enhancement is guarded with @supports")
def _(ctx):
    if "animation-timeline" in ctx.css and "@supports" not in ctx.css:
        raise Fail("scroll-driven animation is used without a @supports guard")


@check(QUALITY, "!important is not used to paper over specificity")
def _(ctx):
    allowed_block = css_block(ctx.css, "@media (prefers-reduced-motion: reduce)") or ""
    total = ctx.css.count("!important")
    excused = allowed_block.count("!important")
    if total - excused > 0:
        raise Fail(f"{total - excused} unnecessary !important declaration(s)")


# ===========================================================================
# Optional: W3C Nu validator
# ===========================================================================

def run_w3c(verbose):
    import json
    import urllib.request

    print("\n  Sending index.html to https://validator.w3.org/nu/ ...")
    request = urllib.request.Request(
        "https://validator.w3.org/nu/?out=json",
        data=HTML_FILE.read_bytes(),
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "User-Agent": "digital-business-card-checker",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.load(response)
    except Exception as exc:                       # noqa: BLE001
        print(f"  [SKIP] validator unreachable: {exc}")
        return 0

    errors = [m for m in payload.get("messages", []) if m.get("type") == "error"]
    warnings = [m for m in payload.get("messages", []) if m.get("subType") == "warning"]
    for message in errors:
        print(f"  [FAIL] line {message.get('lastLine')}: {message.get('message')}")
    if verbose:
        for message in warnings:
            print(f"  [WARN] line {message.get('lastLine')}: {message.get('message')}")
    if not errors:
        print(f"  [PASS] W3C validator reports 0 errors ({len(warnings)} warnings)")
    return len(errors)


# ===========================================================================
# Runner
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="list passing checks too")
    parser.add_argument("--w3c", action="store_true",
                        help="also validate the markup against the W3C Nu validator "
                             "(sends the file over the network)")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                              # noqa: BLE001
        pass

    ctx = Context()
    passed, failed, warned = 0, [], []
    current_category = None

    print()
    print("=" * 72)
    print("  Digital business card - conformance suite")
    print("=" * 72)

    for category, name, fn in CHECKS:
        if category != current_category:
            current_category = category
            print(f"\n  {category}")
            print("  " + "-" * (len(category)))
        try:
            fn(ctx)
        except Warn as warning:
            warned.append((name, str(warning)))
            print(f"  [WARN] {name}\n         {warning}")
        except Fail as failure:
            failed.append((name, str(failure)))
            print(f"  [FAIL] {name}\n         {failure}")
        except Exception as error:                 # noqa: BLE001
            failed.append((name, f"{type(error).__name__}: {error}"))
            print(f"  [FAIL] {name}\n         {type(error).__name__}: {error}")
        else:
            passed += 1
            if args.verbose:
                print(f"  [PASS] {name}")

    w3c_errors = run_w3c(args.verbose) if args.w3c else 0

    print()
    print("=" * 72)
    print(f"  {passed} passed | {len(failed)} failed | {len(warned)} warnings")
    if failed:
        print()
        for name, reason in failed:
            print(f"  FAILED: {name} - {reason}")
    print("=" * 72)
    print()

    return 1 if failed or w3c_errors else 0


if __name__ == "__main__":
    sys.exit(main())
