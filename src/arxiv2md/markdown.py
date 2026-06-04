"""Convert arXiv HTML to Markdown with a custom serializer."""

from __future__ import annotations

import re
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
    from bs4.element import NavigableString, Tag
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise RuntimeError("BeautifulSoup4 is required for HTML parsing (pip install beautifulsoup4).") from exc


_EQUATION_TABLE_RE = re.compile(r"ltx_equationgroup|ltx_eqn_align|ltx_eqn_table")
_EQUATION_NUMBER_RE = re.compile(r"(.*?)(\([0-9]+\))([^\$]*)")
_LATEX_PREFIX_SUFFIX = "%@math@%"
_STRIP_LATEX_COMMANDS = [
    r"\\leavevmode",
    r"\\nobreak",
    r"\\relax",
    r"\\ignorespaces",
    r"\\pagebreak",
    r"\\newpage",
    r"\\clearpage",
    r"\\cleardoublepage",
    r"\\allowbreak",
    r"\\samepage",
    r"\\strut",
]

def _substitute_slash_in_latex(m) -> str:
    """
    Handles latex expressions which in MathML annotation are like the below:
    S=\\textit{SV\/}\\rule{0.0pt}{4.30554pt}
    The problematic part is "\/" in the {SV\/} as this is invalid, all such cases
    arrive from the original source where there was a space followed by the "\ ",
    but for some reason conversion from HTML to LaTeX renders it as "\/".

    So we are handling it using this workaround here.

    Parameters
    ----------
    m : re.Match
        Each match object is passed iteratively to this callback method from
        re.sub call

    Returns
    -------
    str
        Returns the replaced string
    """
    return m.group(1) + "\\ " + m.group(3)

_REPLACE_LATEX_COMMANDS = {
    r"\\sans" : r"\\textsf",
    r"\\mbox" : r"\\text",
    r"(?m)(\{[^\\/]*)(\\/)(\})" : _substitute_slash_in_latex,
}


def convert_html_to_markdown(html: str, *, remove_refs: bool = False, remove_toc: bool = False) -> str:
    """Convert arXiv HTML into Markdown."""
    soup = BeautifulSoup(html, "html.parser")
    toc_markdown = None
    toc_nav = soup.find("nav", class_=re.compile(r"ltx_TOC"))
    if toc_nav and not remove_toc:
        toc_markdown = _serialize_toc(toc_nav)

    _strip_unwanted_elements(soup)
    if remove_refs:
        for ref in soup.find_all("section", class_=re.compile(r"ltx_bibliography")):
            ref.decompose()

    convert_all_mathml_to_latex(soup)
    fix_tabular_tables(soup)

    root = _find_document_root(soup)
    title_tag = root.find("h1", class_=re.compile(r"ltx_title_document"))
    authors_tag = root.find("div", class_=re.compile(r"ltx_authors"))
    abstract_tag = root.find("div", class_=re.compile(r"ltx_abstract"))

    blocks: list[str] = []
    if title_tag:
        blocks.append(f"# {_normalize_text(title_tag.get_text(' ', strip=True))}")
    if authors_tag:
        authors_text = _normalize_text(authors_tag.get_text(" ", strip=True))
        if authors_text:
            blocks.append(f"Authors: {authors_text}")
    if toc_markdown:
        blocks.append("## Contents\n" + toc_markdown)
    if abstract_tag:
        blocks.extend(_serialize_abstract(abstract_tag))

    for tag in (title_tag, authors_tag, abstract_tag):
        if tag:
            tag.decompose()

    blocks.extend(_serialize_children(root))

    return _check_and_handle_latex_prefix_suffix(
        "\n\n".join(block for block in blocks if block).strip()
    )


def convert_fragment_to_markdown(html: str, *, remove_inline_citations: bool = False, base_url: str | None = None) -> str:
    """Convert an HTML fragment into Markdown without title/author/abstract handling.

    Parameters
    ----------
    html : str
        The HTML fragment to convert.
    remove_inline_citations : bool
        If True, completely remove inline citation links. If False (default),
        citation links are converted to plain text (URL stripped).
    base_url : str | None
        Base URL to resolve relative image paths against. When provided,
        relative ``<img src>`` attributes are converted to absolute URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    _strip_unwanted_elements(soup)
    convert_all_mathml_to_latex(soup)
    fix_tabular_tables(soup)
    if base_url:
        _resolve_image_urls(soup, base_url)
    blocks = _serialize_children(soup, remove_inline_citations=remove_inline_citations)
    return _check_and_handle_latex_prefix_suffix(
        "\n\n".join(block for block in blocks if block).strip()
    )


def _find_document_root(soup: BeautifulSoup) -> Tag:
    root = soup.find("article", class_=re.compile(r"ltx_document"))
    if root:
        return root
    if soup.body:
        return soup.body
    return soup


def _strip_unwanted_elements(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(["script", "style", "noscript", "link", "meta"]):
        tag.decompose()
    for tag in soup.select("nav.ltx_page_navbar, nav.ltx_TOC"):
        tag.decompose()
    for tag in soup.select("button.sr-only, div.package-alerts, div.ltx_pagination, footer"):
        tag.decompose()

def _check_and_handle_latex_prefix_suffix(md: str) -> str:
    parts = md.split(_LATEX_PREFIX_SUFFIX)
    fixed_md = ""

    def handle_endswith(part: str) -> str:
        # no op is sufficient
        return part

    def handle_startswith(part: str) -> str:
        # inject a space if there is a digit immediately after the
        # terminating $ sign in latex
        if len(part) >= 2 and part[0] == "$" and part[1].isdigit():
            return part[0] + " " + part[1:]
        else:
            return part

    def handle_both(part: str) -> str:
        part = handle_startswith(part)
        part = handle_endswith(part)
        return part

    for idx in range(len(parts)):
        part = parts[idx]
        if part.startswith("$") and part.endswith("$"):
            fixed_md += handle_both(part)
        elif part.endswith("$"):
            fixed_md += handle_endswith(part)
        elif part.startswith("$") and part[1].isdigit():
            fixed_md += handle_startswith(part)
        else:
            fixed_md += part
    return fixed_md


def _correct_multiline_latex_handling(eqn_text: str) -> str:
    eqn_modified = ""
    matches = _EQUATION_NUMBER_RE.findall(eqn_text)
    for match in matches:
        eqn_text, mid, post = match

        eqn_text = eqn_text.strip()
        mid = mid.strip()
        post = post.strip()

        head = "$$"
        tail = "$$"
        if eqn_text.startswith("$$"):
            head = ""
        elif eqn_text.startswith("$"):
            head = "$"

        if eqn_text.endswith("$$"):
            tail = ""
        elif eqn_text.endswith("$"):
            tail = "$"
        eqn_modified += f"{head}{eqn_text}{tail} {mid} {post}\n"
    return eqn_modified
    


def _sanitize_latex_source(latex_source: str) -> str:
    latex_source = re.sub(r"(?<!\\)%", "", latex_source)
    for pattern in _STRIP_LATEX_COMMANDS:
        latex_source = re.sub(pattern, "", latex_source)
    
    for pattern, replacement in _REPLACE_LATEX_COMMANDS.items():
        latex_source = re.sub(pattern, replacement, latex_source)
    
    if "\\text{" in latex_source:
        new_latex = ""
        count = 0
        for ch in latex_source:
            new_latex += ch
            if new_latex.endswith("\\text{") or (count > 0 and new_latex.endswith("{")):
                count += 1
            elif new_latex.endswith("}") and count > 0:
                count -= 1
            elif new_latex.endswith("\\_") or new_latex.endswith("\\^"):
                if count > 0:
                    new_latex = new_latex[0:-2] + "{" + new_latex[-2:] + "}"
                else:
                    new_latex = new_latex[0:-2] + "_"
        return new_latex
    else:
        latex_source = re.sub(r"\\([_^])", r"\1", latex_source)
        latex_source = re.sub(r"\\(?=[\[\]])", "", latex_source)
        return latex_source


def _normalize_pure_text_content(text: str) -> str:
    return text.replace("*", "\\*")

def convert_all_mathml_to_latex(root: BeautifulSoup) -> None:
    for math in root.find_all("math"):
        annotation = math.find("annotation", attrs={"encoding": "application/x-tex"})
        if annotation and annotation.text:
            latex_source = annotation.text.strip()
            latex_source = _sanitize_latex_source(latex_source)
            math.replace_with(f"${_LATEX_PREFIX_SUFFIX}{latex_source}{_LATEX_PREFIX_SUFFIX}$")
        else:
            math.replace_with(math.get_text(" ", strip=True))


def fix_tabular_tables(root: BeautifulSoup) -> None:
    tables = root.find_all("table", class_=re.compile(r"ltx_tabular"))
    for table in tables:
        _remove_all_attributes(table)
        for child in table.find_all(["tbody", "thead", "tfoot", "tr", "td", "th"]):
            _remove_all_attributes(child)


def _resolve_image_urls(root: BeautifulSoup, base_url: str) -> None:
    """Resolve relative ``<img src>`` attributes to absolute URLs."""
    # Ensure base_url ends with '/' so urljoin resolves relative paths correctly
    if not base_url.endswith("/"):
        base_url += "/"
    
    arxiv_id = base_url.split("/")[-2]

    if "ar5iv.labs.arxiv.org" in base_url:
        base_url = "https://ar5iv.labs.arxiv.org"
        arxiv_id = ""

    for img in root.find_all("img"):
        src = img.get("src")
        if src and not src.startswith(("http://", "https://", "data:")):
            if arxiv_id and src.startswith(arxiv_id):
                # most of papers have the image src values that already have the
                # form of {arxiv_id}/{fig_name} and not just {fig_name}
                # so the base url essentially in that case needs to be only
                # something like https://arxiv.org/html/ and so we remove the
                # trailing '/' combined with urljoin's behavior
                #
                # if the base_url is an ar5iv url then the image urls are
                # relative to the base domain which we hardcode and that case
                # will always operate through the 'else' path
                img["src"] = urljoin(base_url[:-1], src)
            else:
                img["src"] = urljoin(base_url, src)


def _remove_all_attributes(tag: Tag) -> None:
    keep_attr = ["rowspan", "colspan", "class"]
    tag.attrs = {k: v for k, v in tag.attrs.items() if k in keep_attr}


def _serialize_children(container: Tag, *, remove_inline_citations: bool = False) -> list[str]:
    blocks: list[str] = []
    for child in container.children:
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue
        blocks.extend(_serialize_block(child, remove_inline_citations=remove_inline_citations))
    return blocks


def _serialize_block(tag: Tag, *, remove_inline_citations: bool = False) -> list[str]:
    if tag.name in {"section", "article", "div", "span"}:
        return _serialize_children(tag, remove_inline_citations=remove_inline_citations)

    if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag.name[1])
        heading = _normalize_text(tag.get_text(" ", strip=True))
        if not heading:
            return []
        return [f"{'#' * level} {heading}"]

    if tag.name == "p":
        paragraph = _serialize_paragraph(tag, remove_inline_citations=remove_inline_citations)
        return [paragraph] if paragraph else []

    if tag.name in {"ul", "ol"}:
        lines = _serialize_list(tag, remove_inline_citations=remove_inline_citations)
        return ["\n".join(lines)] if lines else []

    if tag.name == "figure":
        figure = _serialize_figure(tag, remove_inline_citations=remove_inline_citations)
        return [figure] if figure else []

    if tag.name == "table":
        table_md = _serialize_table(tag, remove_inline_citations=remove_inline_citations)
        return [table_md] if table_md else []

    if tag.name == "blockquote":
        content = _normalize_text(_serialize_inline(tag, remove_inline_citations=remove_inline_citations))
        if not content:
            return []
        return ["> " + content]

    if tag.name == "br":
        return []

    return _serialize_children(tag, remove_inline_citations=remove_inline_citations)


def _serialize_abstract(tag: Tag) -> list[str]:
    blocks = ["## Abstract"]
    paragraphs = tag.find_all("p")
    if not paragraphs:
        content = _normalize_text(tag.get_text(" ", strip=True))
        if content:
            blocks.append(content)
        return blocks

    for paragraph in paragraphs:
        text = _serialize_paragraph(paragraph)
        if text:
            blocks.append(text)
    return blocks


def _serialize_paragraph(tag: Tag, *, remove_inline_citations: bool = False) -> str:
    content = _serialize_inline(tag, remove_inline_citations=remove_inline_citations)
    content = _cleanup_inline_text(content)
    return content


def _is_citation_link(href: str | None) -> bool:
    """Check if a link is a citation reference (e.g., #bib.bib7)."""
    if not href:
        return False
    return "#bib." in href or href.startswith("#bib")

def _is_invalid_citation(cite_tag):
    children = [child for child in cite_tag.children if child.name is not None]
    
    return bool(children) and all(
        child.name == 'span' and
        'ltx_missing_citation' in child.get('class', []) and
        'ltx_ref_self' in child.get('class', [])
        for child in children
    )


def _is_internal_paper_link(href: str | None) -> bool:
    """Check if a link is an internal paper section reference (e.g., arxiv.org/html/...#S2.SS1)."""
    if not href:
        return False
    return "arxiv.org/html/" in href and "#" in href and "#bib" not in href


def _serialize_inline(node: Tag | NavigableString, *, remove_inline_citations: bool = False, indent: int = 0, nested_table: bool = False) -> str:
    if isinstance(node, NavigableString):
        return _normalize_pure_text_content(str(node))

    if node.name == "br":
        return "\n"
    
    if "ltx_ERROR" in node.get("class", []):
        return ""

    if node.name in {"em", "i"} or "ltx_font_italic" in node.get("class", []):
        return f"*{_serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent).strip()}*"

    if "ltx_font_bold" in node.get("class", []) and "ltx_font_typewriter" in node.get("class", []):
        code_block = _serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent).replace('\\*', '*')
        return f"**`{code_block}`**"

    if node.name in {"strong", "b"} or "ltx_font_bold" in node.get("class", []):
        return f"**{_serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent).strip()}**"
    
    if node.name == "a":
        text = _serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent).strip()
        href = node.get("href")
        # Handle citation links specially
        if _is_citation_link(href):
            if remove_inline_citations:
                return ""  # Completely remove citation
            return text  # Keep text only, strip URL
        # Handle internal paper links (section references)
        if remove_inline_citations and _is_internal_paper_link(href):
            return text  # Keep text only, strip URL
        # Regular links: keep full markdown link
        if href:
            return f"[{text or href}]({href})"
        return text

    if node.name == "sup":
        text = _serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent).strip()
        return f"^{text}" if text else ""

    if node.name == "cite":
        if _is_invalid_citation(node):
            return ""
        
        if remove_inline_citations and "ltx_cite" in node.get("class", []):
            return ""
        return _serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent)

    if node.name == "math":
        text = node.get_text(" ", strip=True)
        return f"${text}$" if text else ""

    if "ltx_note" in node.get("class", []):
        text = _normalize_text(_serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent))
        return f"({text})" if text else ""
    
    if "ltx_tag_item" in node.get("class", []):
        return ""
    
    if node.name in {"code"} or "ltx_font_typewriter" in node.get("class", []):
        code_block = _serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent).replace('\\*', '*')
        return f"`{code_block}`"
    
    if node.name in {"ul", "ol"}:
        lines = _serialize_list(node, remove_inline_citations=remove_inline_citations, indent=indent)
        return "\n".join(lines) if lines else ""

    if node.name == "table" and not nested_table:
        table_md = _serialize_table(node, remove_inline_citations=remove_inline_citations)
        return table_md

    return _serialize_children_inline(node, remove_inline_citations=remove_inline_citations, indent=indent, nested_table=nested_table)


def _serialize_children_inline(tag: Tag, *, remove_inline_citations: bool = False, indent: int = 0, nested_table: bool = False) -> str:
    return "".join(_serialize_inline(child, remove_inline_citations=remove_inline_citations, indent=indent, nested_table=nested_table) for child in tag.children)


def _cleanup_inline_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def _serialize_list(list_tag: Tag, indent: int = 0, *, remove_inline_citations: bool = False) -> list[str]:
    lines: list[str] = []
    for item in list_tag.find_all("li", recursive=False):
        item_text_parts: list[str] = []
        nested_lists: list[Tag] = []
        for child in item.children:
            if isinstance(child, Tag) and child.name in {"ul", "ol"}:
                nested_lists.append(child)
            else:
                item_text_parts.append(_serialize_inline(child, remove_inline_citations=remove_inline_citations, indent=indent+1))
        item_text = _cleanup_inline_text("".join(item_text_parts)) if indent else "".join(item_text_parts).strip()
        prefix = "  " * indent + "- "
        lines.append(prefix + item_text if item_text else prefix.rstrip())
        for nested in nested_lists:
            lines.extend(_serialize_list(nested, indent + 1, remove_inline_citations=remove_inline_citations))
    return lines


def _serialize_toc(toc_nav: Tag) -> str:
    list_tag = toc_nav.find("ol")
    if not list_tag:
        return ""
    lines = _serialize_list(list_tag)
    return "\n".join(lines)


def _update_rowspan_info(rowspan_info: dict, values: list[str], row_idx: int) -> list[str]:
    if rowspan_info:
        for col_idx, row_span in rowspan_info.items():
            for (row_start, row_end) in row_span:
                if row_idx > row_start and row_idx < row_end:
                    values.insert(col_idx, "")
    return values

def _serialize_table(table: Tag, *, remove_inline_citations: bool = False) -> str:
    classes = " ".join(table.get("class", []))
    if _EQUATION_TABLE_RE.search(classes):
        eqn_text = _normalize_text(table.get_text(" ", strip=True))
        if not eqn_text:
            return ""
        return _correct_multiline_latex_handling(eqn_text)

    rows = []
    rowspan_info: dict[int, list[tuple[int, int]]] = {}
    # Find rows in tbody, thead, tfoot, or directly in table
    # Handle nested structure where rows might be inside tbody/thead/tfoot
    tbody_elements = table.find_all(["tbody", "thead", "tfoot"], recursive=False)
    
    if tbody_elements:
        # Table has tbody/thead/tfoot structure - find rows within them
        for tbody in tbody_elements:
            for row_idx, row in enumerate(tbody.find_all("tr", recursive=False)):
                cells = row.find_all(["th", "td"], recursive=False)
                if not cells:
                    continue
                values = []
                for col_idx, cell in enumerate(cells):
                    rowspan = cell.get("rowspan", None)
                    if rowspan:
                        value = (row_idx, row_idx + int(rowspan))
                        span_info = rowspan_info.get(col_idx, [])
                        span_info.append(value)

                        rowspan_info[col_idx] = span_info 

                    cell_text = _cleanup_inline_text(_serialize_inline(cell, remove_inline_citations=remove_inline_citations, nested_table=True)).replace("\n", "<br>")
                    values.append(cell_text)
                
                values = _update_rowspan_info(rowspan_info, values, row_idx)
                rows.append(values)
    else:
        # Table has no tbody/thead/tfoot - find rows directly in table
        for row_idx, row in enumerate(table.find_all("tr", recursive=False)):
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            values = []
            for col_idx, cell in enumerate(cells):
                rowspan = cell.get("rowspan", None)
                if rowspan:
                    value = (row_idx, row_idx + int(rowspan))
                    span_info = rowspan_info.get(col_idx, [])
                    span_info.append(value)

                    rowspan_info[col_idx] = span_info
                
                cell_text = _cleanup_inline_text(_serialize_inline(cell, remove_inline_citations=remove_inline_citations, nested_table=True)).replace("\n", "<br>")
                values.append(cell_text)
            
            values = _update_rowspan_info(rowspan_info, values, row_idx)
            rows.append(values)

    if not rows:
        return ""

    max_cols = max(len(row) for row in rows)
    normalized = [row + [""] * (max_cols - len(row)) for row in rows]
    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)

def _serialize_span_table(table: Tag, *, remove_inline_citations: bool = False) -> str:
    classes = " ".join(table.get("class", []))
    if _EQUATION_TABLE_RE.search(classes):
        eqn_text = _normalize_text(table.get_text(" ", strip=True))
        if not eqn_text:
            return ""
        return _correct_multiline_latex_handling(eqn_text)

    rows = []
    rowspan_info: dict[int, list[tuple[int, int]]] = {}
    # Find rows in tbody, thead, tfoot, or directly in table
    # Handle nested structure where rows might be inside tbody/thead/tfoot
    tbody_elements = table.find_all(["tbody", "thead", "tfoot"], recursive=False)
    
    if tbody_elements:
        # Table has tbody/thead/tfoot structure - find rows within them
        for tbody in tbody_elements:
            for row_idx, row in enumerate(tbody.find_all("tr", recursive=False)):
                cells = row.find_all(["th", "td"], recursive=False)
                if not cells:
                    continue
                values = []
                for col_idx, cell in enumerate(cells):
                    rowspan = cell.get("rowspan", None)
                    if rowspan:
                        value = (row_idx, row_idx + int(rowspan))
                        span_info = rowspan_info.get(col_idx, [])
                        span_info.append(value)

                        rowspan_info[col_idx] = span_info 

                    cell_text = _cleanup_inline_text(_serialize_inline(cell, remove_inline_citations=remove_inline_citations, nested_table=True)).replace("\n", "<br>")
                    values.append(cell_text)
                
                values = _update_rowspan_info(rowspan_info, values, row_idx)
                rows.append(values)
    else:
        # Table has no tbody/thead/tfoot - find rows directly in table
        for row_idx, row in enumerate(table.find_all("span", attrs={"class": "ltx_tr"}, recursive=False)):
            cells = row.find_all("span", attrs={"class": "ltx_td"}, recursive=False)
            if not cells:
                continue
            values = []
            for col_idx, cell in enumerate(cells):
                rowspan = cell.get("rowspan", None)
                if rowspan:
                    value = (row_idx, row_idx + int(rowspan))
                    span_info = rowspan_info.get(col_idx, [])
                    span_info.append(value)

                    rowspan_info[col_idx] = span_info
                
                cell_text = _cleanup_inline_text(_serialize_inline(cell, remove_inline_citations=remove_inline_citations, nested_table=True)).replace("\n", "<br>")
                values.append(cell_text)
            
            values = _update_rowspan_info(rowspan_info, values, row_idx)
            rows.append(values)

    if not rows:
        return ""

    max_cols = max(len(row) for row in rows)
    normalized = [row + [""] * (max_cols - len(row)) for row in rows]
    header = normalized[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)

def _serialize_figure(figure: Tag, *, remove_inline_citations: bool = False) -> str:
    # Check if this is a table figure (ltx_table class)
    figure_classes = " ".join(figure.get("class", []))
    is_table_figure = "ltx_table" in figure_classes

    caption_tag = figure.find("figcaption")
    caption = _normalize_text(_serialize_inline(caption_tag, remove_inline_citations=remove_inline_citations)) if caption_tag else ""

    lines = []

    if is_table_figure:
        # Handle table figures - find and serialize the embedded table
        # Note: fix_tabular_tables strips attributes, so search for any table element
        table = figure.find("table")
        if table:
            table_md = _serialize_table(table, remove_inline_citations=remove_inline_citations)
            if caption:
                lines.append(f"**{caption}**")
            if table_md:
                lines.append(table_md)
        elif caption:
            span_table = figure.find("span", attrs={"class": "ltx_tabular"})
            if span_table:
                span_table_md = _serialize_span_table(span_table, remove_inline_citations=remove_inline_citations)
                if caption:
                    lines.append(f"**{caption}**")
                if span_table_md:
                    lines.append(span_table_md)
            else:
                # Fallback if no table found but has caption
                lines.append(f"Table: {caption}")
    else:
        # Handle regular image figures
        img = figure.find("img")
        src = img.get("src") if img else None
        alt = img.get("alt") if img else None
        if src:
            image_label = alt or "Image"
            lines.append(f"![{image_label}]({src})  ")

        if caption:
            if not caption.lower().startswith("figure"):
                lines.append(f"Figure: {caption}")
            else:
                lines.append(f"{caption}")

    return "\n".join(lines).strip()


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
