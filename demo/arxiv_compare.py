"""
arxiv_compare.py  –  PDF / HTML ↔ Markdown side-by-side viewer
Usage:
    streamlit run arxiv_compare.py -- --folder /path/to/markdown/folder

The folder should contain files named <arxiv_id>.md

Left panel  – toggle between:
  • PDF  → https://arxiv.org/pdf/<arxiv_id>.pdf
  • HTML → fetched server-side from arxiv.org/html/<id>, falls back to ar5iv.org/html/<id>

Right panel – toggle between rendered markdown (KaTeX) and raw source.
"""

import argparse
import io
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st

# PIL is used only for favicon loading from a URL
try:
    from PIL import Image as PILImage

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


def favicon_from_url(url: str):
    """Fetch an image URL and return a PIL Image for use as page_icon.
    Falls back to an emoji string if PIL is unavailable or the fetch fails.
    """
    if not _PIL_AVAILABLE:
        return "🚀"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read()
        return PILImage.open(io.BytesIO(data))
    except Exception:
        return "🚀"


# ── Favicon URL — swap this for any reachable PNG/SVG/ICO URL ─────────────────
FAVICON_URL = "https://info.arxiv.org/assets/favicon.png"

# ── Page config ───────────────────────────────────────────────────────────────
# Force light theme via .streamlit/config.toml is the cleanest approach,
# but we also set it here for portability.
st.set_page_config(
    page_title="arXiv · PDF vs Markdown",
    page_icon=favicon_from_url(FAVICON_URL),
    layout="wide",
    initial_sidebar_state="expanded",
)

# Programmatically enforce light mode (works on Streamlit ≥ 1.27)
try:
    st._config.set_option("theme.base", "light")
except Exception:
    pass  # older Streamlit versions — CSS fallback below handles it


# ── CLI argument (folder path) ────────────────────────────────────────────────
def get_folder() -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--folder", default=".")
    args, _ = parser.parse_known_args()
    return Path(args.folder)


FOLDER = get_folder()

# ── URL helpers ───────────────────────────────────────────────────────────────
ARXIV_ID_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")


def is_valid_arxiv_id(stem: str) -> bool:
    return bool(ARXIV_ID_RE.match(stem))


def load_papers(folder: Path) -> dict[str, Path]:
    papers: dict[str, Path] = {}
    if not folder.exists():
        return papers
    for f in sorted(folder.glob("*.md")):
        if is_valid_arxiv_id(f.stem):
            papers[f.stem] = f
    return papers


def pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def abs_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/abs/{arxiv_id}"


# ── Server-side HTML fetch (avoids all CORS issues) ───────────────────────────
HEADERS = {"User-Agent": "Mozilla/5.0 (arxiv-compare/1.0; academic viewer)"}


@st.cache_data(show_spinner=False)
def fetch_html(arxiv_id: str) -> tuple[str, str]:
    """
    Returns (html_source, source_label).
    Tries arxiv.org/html first, falls back to ar5iv.org/html.
    Raises RuntimeError if both fail.
    """
    candidates = [
        (f"https://arxiv.org/html/{arxiv_id}", "arxiv.org/html"),
        (f"https://ar5iv.org/html/{arxiv_id}", "ar5iv.org"),
    ]
    last_err = ""
    for url, label in candidates:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                # Detect encoding from headers or default to utf-8
                content_type = resp.headers.get_content_charset() or "utf-8"
                html = raw.decode(content_type, errors="replace")
                # Treat pages that are just an error notice as failures
                if len(html.strip()) < 500 or (
                    re.search(
                        r"<title>[^<]*(not found|error|unavailable)[^<]*</title>",
                        html,
                        re.I,
                    )
                ):
                    last_err = f"{label}: page looks like an error page"
                    continue

                url_splits = urlsplit(url)
                html = re.sub(
                    'src="([a-zA-Z0-9./\\?=,&-]*)"',
                    f'src="{url_splits.scheme}://{url_splits.netloc}/html/\\1"',
                    html,
                )
                return html, label
        except Exception as exc:
            last_err = f"{label}: {exc}"
            continue
    raise RuntimeError(f"Could not fetch HTML for {arxiv_id}. Last error: {last_err}")


# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
        /* Force light mode colours regardless of OS/browser preference */
        :root, html, body, [data-testid="stAppViewContainer"],
        [data-testid="stSidebar"], .main {
            background-color: #ffffff !important;
            color: #1a1a1a !important;
        }
        [data-testid="stSidebar"] {
            background-color: #f8f9fa !important;
        }
        /* Push content below the fixed Streamlit header (~3.5rem tall) */
        .block-container {
            padding-top: 4.5rem !important;
            padding-bottom: 1rem;
            padding-left: 1rem;
            padding-right: 1rem;
        }

        /* panel section headers */
        .panel-header {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 0.72rem;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: #888;
            padding: 0.35rem 0 0.55rem 0;
            border-bottom: 1px solid #e0e0e0;
            margin-bottom: 0.75rem;
        }

        /* Prevent toggle button labels from wrapping to a second line */
        .stButton > button {
            white-space: nowrap;
        }

        /* sidebar paper list items */
        .paper-meta {
            font-size: 0.78rem;
            color: #999;
            font-family: monospace;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Load papers ───────────────────────────────────────────────────────────────
papers = load_papers(FOLDER)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        "## ![ArXiv Papers](https://assets-public.zohostratus.com/arxiv-logo.png)"
    )
    st.caption(f"Folder path:  \n_{FOLDER.resolve()}_")

    if not papers:
        st.warning(
            "No arXiv markdown files found.\n\n"
            "Make sure the folder contains files named `<arxiv_id>.md` "
            "(e.g. `2310.06825.md`)."
        )
        st.stop()

    paper_ids = list(papers.keys())
    selected_id = st.selectbox(
        "Paper list",
        paper_ids,
        format_func=lambda x: x,
        label_visibility="visible",
    )
    st.markdown(
        f'<p class="paper-meta">arXiv:{selected_id}</p>'
        f'<a href="{abs_url(selected_id)}" target="_blank" '
        f'style="font-size:0.78rem;">Open abstract ↗</a>',
        unsafe_allow_html=True,
    )
    st.divider()
    panel_height = st.slider("Panel height (px)", 400, 1200, 800, step=25)

# ── Main: two-column comparison ───────────────────────────────────────────────
col_pdf, col_md = st.columns(2, gap="medium")

# ── Left: PDF / HTML toggle ───────────────────────────────────────────────────
with col_pdf:
    p_col1, p_col2, _ = st.columns([1.1, 1.4, 3.5])
    with p_col1:
        if st.button("PDF", use_container_width=True):
            st.session_state["pdf_view"] = "pdf"
    with p_col2:
        if st.button("HTML", use_container_width=True):
            st.session_state["pdf_view"] = "html"
    if "pdf_view" not in st.session_state:
        st.session_state["pdf_view"] = "html"

    pdf_view = st.session_state["pdf_view"]
    plabel = "✦ PDF" if pdf_view == "pdf" else "✦ HTML"
    st.markdown(
        f'<div class="panel-header">📄&nbsp; arXiv source &nbsp;·&nbsp; {plabel}</div>',
        unsafe_allow_html=True,
    )

    if pdf_view == "pdf":
        url = pdf_url(selected_id)
        st.components.v1.iframe(url, height=panel_height, scrolling=True)
        st.caption(f"[{url}]({url})")

    else:
        # ── Fetch HTML server-side ─────────────────────────────────────────
        with st.spinner("Fetching HTML…"):
            try:
                html_src, src_label = fetch_html(selected_id)
                fetch_error = None
            except RuntimeError as e:
                html_src, src_label, fetch_error = "", "", str(e)

        if fetch_error:
            st.error(f"Could not load HTML view: {fetch_error}")
            st.info(
                f"You can open the paper directly: "
                f"[arxiv.org/html/{selected_id}](https://arxiv.org/html/{selected_id})"
            )
        else:
            # Inject the full HTML page into a sandboxed srcdoc iframe so that
            # relative asset URLs (CSS, images) resolve against the source domain.
            # We rewrite <head> to add a <base> tag pointing at the origin.
            origin = (
                "https://arxiv.org" if "arxiv.org" in src_label else "https://ar5iv.org"
            )
            base_tag = f'<base href="{origin}/">'
            # Insert base tag right after <head> (case-insensitive)
            patched = re.sub(
                r"(<head\b[^>]*>)",
                r"\1" + base_tag,
                html_src,
                count=1,
                flags=re.I,
            )
            # Escape for use inside a JS template literal / srcdoc attribute
            # srcdoc uses HTML attribute encoding; escape & " only
            srcdoc_safe = patched.replace("&", "&amp;").replace('"', "&quot;")

            st.components.v1.html(
                f"""
                <style>
                    body, html {{ margin: 0; padding: 0; }}
                    iframe {{
                        width: 100%;
                        height: {panel_height}px;
                        border: 1px solid #ddd;
                        border-radius: 4px;
                        display: block;
                    }}
                </style>
                <iframe srcdoc="{srcdoc_safe}"
                        sandbox="allow-scripts allow-same-origin allow-popups"
                        loading="lazy">
                </iframe>
                """,
                height=panel_height + 4,
                scrolling=False,
            )
            st.caption(f"Source: **{src_label}** — {selected_id}")

# ── Right: Markdown ───────────────────────────────────────────────────────────
with col_md:
    t_col1, t_col2, _ = st.columns([1.6, 1.1, 3.3])
    with t_col1:
        if st.button("Rendered", use_container_width=True):
            st.session_state["md_view"] = "rendered"
    with t_col2:
        if st.button("Source", use_container_width=True):
            st.session_state["md_view"] = "source"
    if "md_view" not in st.session_state:
        st.session_state["md_view"] = "rendered"

    view = st.session_state["md_view"]
    label = "✦ Rendered" if view == "rendered" else "✦ Source"
    st.markdown(
        f'<div class="panel-header">📝&nbsp; Markdown &nbsp;·&nbsp; {label}</div>',
        unsafe_allow_html=True,
    )

    md_path: Path = papers[selected_id]
    md_text = md_path.read_text(encoding="utf-8")

    if view == "rendered":
        # markdown-it + markdown-it-texmath (KaTeX) + markdown-it-footnote
        # Math:      $...$ inline, $$...$$ display, \(...\), \[...\]
        # Footnotes: [^label] inline ref, [^label]: definition block
        st.components.v1.html(
            f"""
            <link rel="stylesheet"
                  href="https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css">
            <script defer
                    src="https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.js"></script>
            <script src="https://cdn.jsdelivr.net/npm/markdown-it@14/dist/markdown-it.min.js"></script>
            <script src="https://cdn.jsdelivr.net/npm/markdown-it-texmath@1/texmath.min.js"></script>
            <script src="https://cdn.jsdelivr.net/npm/markdown-it-footnote@3/dist/markdown-it-footnote.min.js"></script>

            <style>
                body {{
                    margin: 0;
                    padding: 0.5rem 1rem;
                    font-family: 'Ubuntu', serif;
                    font-size: 15px;
                    line-height: 1.75;
                    color: #1a1a1a;
                    box-sizing: border-box;
                }}
                h1,h2,h3,h4,h5 {{
                    font-family: Ubuntu, serif;
                    margin-top: 1.4em;
                    margin-bottom: 0.4em;
                }}
                p {{ margin: 0.6em 0; }}
                pre {{
                    background: #f6f8fa;
                    padding: .8rem;
                    border-radius: 4px;
                    overflow-x: auto;
                    font-size: 0.82rem;
                    font-family: 'IBM Plex Mono', monospace;
                }}
                code {{
                    background: #f0f0f0;
                    padding: .1em .3em;
                    border-radius: 3px;
                    font-size: 0.85em;
                    font-family: 'IBM Plex Mono', monospace;
                }}
                pre code {{ background: none; padding: 0; font-size: inherit; }}
                blockquote {{
                    border-left: 3px solid #ccc;
                    margin-left: 0;
                    padding-left: 1rem;
                    color: #555;
                }}
                table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
                th, td {{ border: 1px solid #ddd; padding: .4rem .7rem; text-align: left; }}
                th {{ background: #f6f8fa; }}
                img {{ max-width: 100%; }}
                .katex-display {{ overflow-x: auto; overflow-y: hidden; padding: 0.2em 0; }}
                .katex-error {{ color: #cc0000; }}
                /* ── Footnotes ── */
                .footnotes-sep {{
                    border: none;
                    border-top: 1px solid #ddd;
                    margin: 2em 0 1em;
                }}
                .footnotes {{
                    font-size: 0.82em;
                    color: #555;
                    line-height: 1.6;
                }}
                .footnotes ol {{
                    padding-left: 1.4em;
                }}
                .footnotes li {{ margin-bottom: 0.35em; }}
                /* back-reference arrow */
                .footnote-backref {{
                    font-style: normal;
                    margin-left: 0.25em;
                    text-decoration: none;
                    color: #888;
                }}
                /* inline superscript anchor */
                .footnote-ref a {{
                    text-decoration: none;
                    color: #0969da;
                    font-size: 0.78em;
                    vertical-align: super;
                    line-height: 0;
                }}
            </style>

            <div id="content"></div>

            <script>
            function renderWhenReady() {{
                if (typeof katex === 'undefined' || typeof texmath === 'undefined' || typeof markdownitFootnote === 'undefined') {{
                    setTimeout(renderWhenReady, 50);
                    return;
                }}
                const mdit = markdownit({{ html: true, linkify: true, typographer: true }})
                    .use(texmath, {{
                        engine: katex,
                        delimiters: ['dollars', 'brackets'],
                        katexOptions: {{ throwOnError: false, output: 'html' }}
                    }})
                    .use(markdownitFootnote);
                const mdSource = {repr(md_text)};
                document.getElementById('content').innerHTML = mdit.render(mdSource);
            }}
            renderWhenReady();
            </script>
            """,
            height=panel_height,
            scrolling=True,
        )
    else:
        st.components.v1.html(
            f"""
            <style>
                body {{ margin: 0; }}
                pre {{
                    margin: 0;
                    background: #f6f8fa;
                    padding: 1rem 1.2rem;
                    font-family: 'IBM Plex Mono', 'Fira Code', monospace;
                    font-size: 0.82rem;
                    line-height: 1.65;
                    white-space: pre-wrap;
                    word-break: break-word;
                    color: #24292f;
                    border: 1px solid #ddd;
                    border-radius: 4px;
                    box-sizing: border-box;
                    height: {panel_height}px;
                    overflow-y: auto;
                }}
            </style>
            <pre>{md_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")}</pre>
            """,
            height=panel_height,
            scrolling=False,
        )

    st.download_button(
        label="⬇ Download .md",
        data=md_text,
        file_name=f"{selected_id}.md",
        mime="text/markdown",
    )
