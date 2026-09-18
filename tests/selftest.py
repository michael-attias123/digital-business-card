#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mutation test for tests/check.py.

A conformance suite that passes no matter what is worthless.  This script
copies the project into a temporary directory, deliberately breaks one thing
at a time, and asserts that check.py notices.  Every mutation below maps to a
requirement in the assignment brief.

    python tests/selftest.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (label, file, find, replace[, count])  - `find` must occur in the pristine
# file; `count` defaults to 1, use -1 to rewrite every occurrence.
MUTATIONS = [
    ("a <script> tag sneaks in",
     "index.html", "</body>", "<script>alert(1)</script></body>"),

    ("an inline event handler sneaks in",
     "index.html", '<main id="main">', '<main id="main" onclick="x()">'),

    ("a javascript: URL sneaks in",
     "index.html", 'href="#about"', 'href="javascript:void(0)"'),

    ('an inline style="" sneaks in',
     "index.html", '<footer class="footer">', '<footer class="footer" style="color:red">'),

    ("the external stylesheet is unlinked",
     "index.html", '<link rel="stylesheet" href="css/style.css">', ""),

    ("the LinkedIn link is removed",
     "index.html", "https://www.linkedin.com/in/michael-attias", "https://example.com/", -1),

    ("the GitHub link is removed",
     "index.html", "https://github.com/michael-attias123", "https://example.com/", -1),

    ("the phone link is removed",
     "index.html", 'href="tel:+972501234567"', 'href="#contact"', -1),

    ("the email link is removed",
     "index.html", 'href="mailto:michael@notreal.com"', 'href="#contact"', -1),

    ("the profile image loses its alt text",
     "index.html", 'alt="תמונת הפרופיל', 'data-alt="תמונת הפרופיל'),

    ("the profile image points at a missing file",
     "index.html", 'src="assets/avatar.svg"', 'src="assets/missing.png"'),

    ("a nav link points at a section that does not exist",
     "index.html", 'href="#skills"', 'href="#kills"'),

    ("an SVG icon reference is misspelled",
     "index.html", 'use href="#i-github"', 'use href="#i-guthub"'),

    ("a tag is left unclosed",
     "index.html", "</address>", ""),

    ("a second <h1> appears",
     "index.html", '<h2 class="section__title" id="about-title">',
     '<h1 class="section__title" id="about-title">'),

    ("the viewport meta tag disappears",
     "index.html", '<meta name="viewport" content="width=device-width, initial-scale=1">', ""),

    ("pinch-zoom gets disabled",
     "index.html", 'content="width=device-width, initial-scale=1"',
     'content="width=device-width, initial-scale=1, user-scalable=no"'),

    ("an id is duplicated",
     "index.html", 'id="contact-title"', 'id="about-title"'),

    ("a section loses its accessible name",
     "index.html", ' aria-labelledby="skills-title"', ""),

    ("an external link loses rel=noopener",
     "index.html", 'rel="noopener noreferrer me"', 'rel="me"'),

    ("Hebrew stops being the default language",
     "index.html", 'id="lang-he" checked', 'id="lang-he"'),

    ("an English string loses its translation",
     "index.html",
     '<span class="en" lang="en">Michael Attias</span>', ""),

    ("an English string loses its lang attribute",
     "index.html", '<span class="en" lang="en">About</span>',
     '<span class="en">About</span>'),

    ("the theme checkbox becomes a plain div",
     "index.html", '<input class="switch" type="checkbox" id="theme-switch">',
     '<div class="switch" id="theme-switch"></div>'),

    ("the dark-mode rules are deleted",
     "css/style.css", "#theme-switch:checked ~ .page {", "#nope:checked ~ .page {"),

    ("the media queries are deleted",
     "css/style.css", "@media (min-width:", "@supports (min-width:", -1),

    ("a fixed desktop width creeps into the layout",
     "css/style.css", ".container {", ".container { width: 1200px;"),

    ("secondary text loses its contrast",
     "css/style.css", "--fg-muted: #5f687e;", "--fg-muted: #b9bfcc;"),

    ("dark-mode text loses its contrast",
     "css/style.css", "--fg-muted: #8b95ad;", "--fg-muted: #3b4257;"),

    ("a var() points at a token that no longer exists",
     "css/style.css", "var(--fg-muted)", "var(--fg-mutted)"),

    ("the print stylesheet is removed",
     "css/style.css", "@media print {", "@supports print {"),

    ("the reduced-motion block is removed",
     "css/style.css", "@media (prefers-reduced-motion: reduce) {",
     "@supports (prefers-reduced-motion: reduce) {"),

    ("focus outlines are removed",
     "css/style.css", ":focus-visible", ":hover-nope", -1),

    ("the RTL/LTR flip is removed",
     "css/style.css", "direction: ltr;", "direction: rtl;", -1),

    ("a class in the HTML has no matching CSS rule",
     "index.html", 'class="hero__tagline"', 'class="hero__taglien"'),
]


def run_suite(project: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(project / "tests" / "check.py")],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                              # noqa: BLE001
        pass

    print()
    print("=" * 72)
    print("  Mutation test - does the suite actually catch regressions?")
    print("=" * 72)
    print()

    caught, missed, invalid = 0, [], []

    with tempfile.TemporaryDirectory() as tmp:
        pristine = Path(tmp) / "pristine"
        shutil.copytree(
            ROOT, pristine,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.zip"),
        )

        baseline = run_suite(pristine)
        if baseline.returncode != 0:
            print("  The pristine project does not pass its own suite - fix that first.")
            print(baseline.stdout[-2000:])
            return 1
        print("  [BASELINE] the untouched project passes\n")

        for label, relative, find, replace, *rest in MUTATIONS:
            count = rest[0] if rest else 1
            work = Path(tmp) / "work"
            if work.exists():
                shutil.rmtree(work)
            shutil.copytree(pristine, work)

            target = work / relative
            source = target.read_text(encoding="utf-8")
            if find not in source:
                invalid.append(label)
                print(f"  [BROKEN MUTATION] {label}: pattern not found in {relative}")
                continue

            target.write_text(source.replace(find, replace, count), encoding="utf-8")
            result = run_suite(work)

            if result.returncode != 0:
                caught += 1
                reasons = re.findall(r"FAILED: (.+?) - ", result.stdout)
                detail = reasons[0] if reasons else "?"
                print(f"  [CAUGHT] {label}")
                print(f"           -> {detail}")
            else:
                missed.append(label)
                print(f"  [MISSED] {label}")

    print()
    print("=" * 72)
    print(f"  {caught} caught | {len(missed)} missed | {len(invalid)} invalid mutations")
    if missed:
        print()
        for label in missed:
            print(f"  NOT CAUGHT: {label}")
    print("=" * 72)
    print()
    return 1 if missed or invalid else 0


if __name__ == "__main__":
    sys.exit(main())
