#!/usr/bin/env python3
"""
Convert a Markdown file (or an HTML file) to PDF, rendering ```mermaid blocks
as diagrams.

Pipeline: Markdown -> HTML (python-markdown) -> headless Chromium (Playwright)
loads the page, mermaid.js renders diagrams in-browser -> page.pdf().

Setup:
    pip install markdown pygments playwright
    # Uses the Microsoft Edge / Chrome already installed. If neither is found:
    playwright install chromium

Usage:
    python md2pdf.py report.md
    python md2pdf.py report.md -o out.pdf --author "Pramod" --toc
    python md2pdf.py report.md --title "My Report" --version v1.2 --date 2026-10-02
    python md2pdf.py report.md --no-header-footer
    python md2pdf.py report.md --mermaid-js ./mermaid.min.js   # offline
    python md2pdf.py page.html                 # HTML -> PDF (.html / .htm)

Optional front matter at the top of the .md (CLI flags override it):
    ---
    title: Assembly position writer
    author: Pramod
    date: 2026-10-02
    version: v1.0
    toc: true
    toc_depth: 3
    ---

Title: taken from --title, else front matter, else the first "# Heading"
(which is then removed from the body so it isn't printed twice).
"""
import argparse
import datetime
import html
import re
import sys
from pathlib import Path

import markdown
from playwright.sync_api import sync_playwright

MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"

CSS = """
@page { size: A4; }
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       font-size: 11pt; line-height: 1.5; color: #1f2328; }
h1, h2, h3, h4 { line-height: 1.25; margin-top: 1.4em; break-after: avoid; }
h2 { border-bottom: 1px solid #eaeef2; padding-bottom: .2em; }
code { font-family: Consolas, "Courier New", monospace; font-size: 90%;
       background: #f3f4f6; padding: .1em .3em; border-radius: 3px; }
pre { background: #f6f8fa; padding: 10px 12px; border-radius: 6px;
      white-space: pre-wrap; word-break: break-word; break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; margin: 1em 0; }
th, td { border: 1px solid #d0d7de; padding: 5px 10px; }
th { background: #f6f8fa; }
blockquote { margin: 1em 0; padding: 0 1em; color: #57606a;
             border-left: 4px solid #d0d7de; }
img { max-width: 100%; }
a { color: #0969da; text-decoration: none; }

/* Mermaid: reset <pre> styling (overflow/background caused blank pages) and
   cap the height so a tall flowchart always fits on a single page. */
pre.mermaid { background: none; padding: 0; margin: 1.2em 0; overflow: visible;
              white-space: normal; text-align: center; break-inside: avoid; }
pre.mermaid svg { max-width: 100%; max-height: 225mm; height: auto; }

/* Title block + TOC */
.doc-header { border-bottom: 2px solid #1f2328; padding-bottom: 10px; margin-bottom: 18px; }
.doc-title { font-size: 26pt; margin: 0 0 4px 0; border: none; padding: 0; }
.doc-meta { color: #57606a; font-size: 10.5pt; }
.toc { background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px;
       padding: 6px 18px 10px; margin: 0 0 22px; break-inside: avoid; }
.toc-title { margin: 8px 0 4px; font-size: 13pt; border: none; }
.toc ul { list-style: none; margin: 0; padding-left: 1.1em; }
.toc > ul { padding-left: 0; }
.toc li { margin: 2px 0; }
"""


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_front_matter(text: str):
    m = re.match(r"\ufeff?---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*\n", text, re.S)
    meta = {}
    if not m:
        return meta, text
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, text[m.end():]


def truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def extract_mermaid(md_text: str):
    """Swap ```mermaid fences for placeholders so the md parser leaves them alone."""
    blocks = []

    def repl(m):
        blocks.append(m.group(1).strip("\n"))
        return f"\n\nMERMAIDPLACEHOLDER{len(blocks) - 1}END\n\n"

    pattern = re.compile(r"^```mermaid[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)
    return pattern.sub(repl, md_text), blocks


def render_toc(tokens, max_level: int) -> str:
    items = []
    for t in tokens:
        if t["level"] > max_level:
            continue
        name = html.escape(html.unescape(t["name"]))
        kids = render_toc(t["children"], max_level)
        items.append(f'<li><a href="#{t["id"]}">{name}</a>{kids}</li>')
    return f"<ul>{''.join(items)}</ul>" if items else ""


# --------------------------------------------------------------------------- #
# HTML build
# --------------------------------------------------------------------------- #
def build_html(md_text: str, opts: dict, mermaid_src: str):
    meta, md_text = parse_front_matter(md_text)

    def pick(key, default=None):
        v = opts.get(key)
        return v if v not in (None, "") else meta.get(key, default)

    title = pick("title")
    if not title:
        m = re.match(r"\s*#[ \t]+(.+?)[ \t#]*\n", md_text)
        if m:
            title = m.group(1).strip()
            md_text = md_text[m.end():]
    title = title or opts["stem"]

    author = pick("author", "")
    version = pick("version", "")
    date = pick("date", datetime.date.today().isoformat())
    show_toc = opts.get("toc") or truthy(meta.get("toc", "false"))
    toc_depth = int(opts.get("toc_depth") or meta.get("toc_depth", 3))

    md_text, blocks = extract_mermaid(md_text)
    md = markdown.Markdown(
        extensions=[
            "markdown.extensions.extra",
            "markdown.extensions.toc",
            "markdown.extensions.sane_lists",
            "markdown.extensions.codehilite",
        ],
        extension_configs={
            "markdown.extensions.codehilite": {"guess_lang": False, "noclasses": True}
        },
    )
    body = md.convert(md_text)
    for i, src in enumerate(blocks):
        body = body.replace(
            f"<p>MERMAIDPLACEHOLDER{i}END</p>",
            f'<pre class="mermaid">{html.escape(src)}</pre>',
        )

    meta_line = " · ".join(x for x in [author, date, version] if x)
    header = (
        f'<header class="doc-header"><h1 class="doc-title">{html.escape(title)}</h1>'
        f'<div class="doc-meta">{html.escape(meta_line)}</div></header>'
    )

    toc_html = ""
    if show_toc and md.toc_tokens:
        top = min(t["level"] for t in md.toc_tokens)
        inner = render_toc(md.toc_tokens, top + toc_depth - 1)
        if inner:
            toc_html = (
                f'<nav class="toc"><h2 class="toc-title">{html.escape(opts["toc_title"])}</h2>'
                f"{inner}</nav>"
            )

    doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{CSS}</style>
<script src="{mermaid_src}"></script>
</head><body>
{header}
{toc_html}
{body}
<script>
  window.mermaidDone = false;
  mermaid.initialize({{ startOnLoad: false, theme: "default", securityLevel: "loose" }});
  mermaid.run({{ querySelector: ".mermaid" }})
    .then(() => {{ window.mermaidDone = true; }})
    .catch(e => {{ console.error(e); window.mermaidDone = true; }});
</script>
</body></html>"""
    return doc, {"title": title, "author": author, "date": date, "version": version}


def header_footer_templates(info: dict):
    base = (
        "font-size:8px;color:#666;width:100%;padding:0 16mm;"
        "display:flex;justify-content:space-between;"
        "font-family:Helvetica,Arial,sans-serif;"
    )
    e = html.escape
    right_head = " · ".join(x for x in [info["author"], info["version"]] if x)
    header = (
        f'<div style="{base}"><span>{e(info["title"])}</span>'
        f"<span>{e(right_head)}</span></div>"
    )
    footer = (
        f'<div style="{base}"><span>{e(info["date"])}</span>'
        '<span>Page <span class="pageNumber"></span> of '
        '<span class="totalPages"></span></span></div>'
    )
    return header, footer


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #
HTML_EXT = {".html", ".htm"}


def html_info(html_text: str, opts: dict, stem: str) -> dict:
    """Header/footer info for an HTML input (title from <title> unless overridden)."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.S | re.I)
    page_title = html.unescape(m.group(1)).strip() if m else ""
    return {
        "title": opts.get("title") or page_title or stem,
        "author": opts.get("author") or "",
        "version": opts.get("version") or "",
        "date": opts.get("date") or datetime.date.today().isoformat(),
    }


def pdf_options(info: dict, use_hf: bool) -> dict:
    """Keyword arguments for Playwright's page.pdf() (shared by CLI and GUI)."""
    kw = dict(
        format="A4",
        print_background=True,
        margin={
            "top": "22mm" if use_hf else "16mm",
            "bottom": "20mm" if use_hf else "16mm",
            "left": "16mm",
            "right": "16mm",
        },
    )
    if use_hf:
        head, foot = header_footer_templates(info)
        kw.update(display_header_footer=True, header_template=head, footer_template=foot)
    return kw


def launch_browser(p):
    """Prefer the installed Edge/Chrome; fall back to Playwright's own Chromium."""
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel)
        except Exception:
            continue
    return p.chromium.launch()


def convert(src: Path, pdf_path: Path, opts: dict):
    is_html = src.suffix.lower() in HTML_EXT
    use_hf = not opts.get("no_header_footer")
    tmp_html = None

    if is_html:
        # Print the HTML file as-is (its own CSS, images and scripts apply).
        url = src.resolve().as_uri()
        info = html_info(src.read_text(encoding="utf-8", errors="replace"), opts, src.stem)
    else:
        mermaid_src = (
            Path(opts["mermaid_js"]).resolve().as_uri() if opts.get("mermaid_js") else MERMAID_CDN
        )
        html_doc, info = build_html(
            src.read_text(encoding="utf-8"), {**opts, "stem": src.stem}, mermaid_src
        )
        # Write next to the source so relative image paths in the md still work.
        tmp_html = src.with_suffix(".tmp.html")
        tmp_html.write_text(html_doc, encoding="utf-8")
        url = tmp_html.resolve().as_uri()

    try:
        with sync_playwright() as p:
            browser = launch_browser(p)
            page = browser.new_page()
            page.on(
                "console",
                lambda msg: msg.type == "error" and print("[browser]", msg.text, file=sys.stderr),
            )
            page.goto(url, wait_until="load")
            if is_html:
                try:
                    page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
            else:
                page.wait_for_function("window.mermaidDone === true", timeout=60_000)
            page.pdf(path=str(pdf_path), **pdf_options(info, use_hf))
            browser.close()
    finally:
        if tmp_html:
            tmp_html.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description="Markdown or HTML -> PDF (Mermaid diagrams supported)")
    ap.add_argument("input", type=Path, help=".md / .markdown / .html / .htm file")
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--title", help="Default: front matter, first # heading, or <title> for HTML")
    ap.add_argument("--author")
    ap.add_argument("--date", help="Default: today (ISO format)")
    ap.add_argument("--version", help="e.g. v1.0")
    ap.add_argument("--toc", action="store_true", help="Add a table of contents (Markdown only)")
    ap.add_argument("--toc-depth", type=int, help="Heading levels in TOC (default 3)")
    ap.add_argument("--toc-title", default="Contents")
    ap.add_argument("--no-header-footer", action="store_true",
                    help="Disable running header and page-number footer")
    ap.add_argument("--mermaid-js", help="Path to a local mermaid.min.js (offline use)")
    args = ap.parse_args()

    out = args.output or args.input.with_suffix(".pdf")
    convert(args.input, out, vars(args))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
