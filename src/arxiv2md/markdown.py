"""Convert arXiv HTML to Markdown with a custom serializer."""

from __future__ import annotations

import json
import re
from base64 import b64decode
from urllib.parse import urljoin

try:
    from bs4 import BeautifulSoup
    from bs4.element import NavigableString, Tag
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise RuntimeError(
        "BeautifulSoup4 is required for HTML parsing (pip install beautifulsoup4)."
    ) from exc


_EQUATION_TABLE_RE = re.compile(r"ltx_equationgroup|ltx_eqn_align|ltx_eqn_table")

# in the below regex, the part \([0-9A-Za-z]+\) is still not complete in the sense
# does not cover all edge cases already known, for example if there is a special
# character or LaTeX expression in the equation numbering like the below:
# $$\quad\eta_{3}=\eta\cdot\frac{1}{d}, ( $\mu$ P)$$
#
# ideally the above should be changed to
# $$\quad\eta_{3}=\eta\cdot\frac{1}{d}, ( $\mu$ P)$$
_EQUATION_NUMBER_RE = re.compile(
    r"(.*?)(\([0-9A-Za-z\.]+\))(?![^%]*%@math_en@%)([^\$]*)"
)
_LATEX_PREFIX = "%@math_st@%"
_LATEX_SUFFIX = "%@math_en@%"
_STRIP_LATEX_COMMANDS = [
    r"\\allowbreak",
    r"\\cleardoublepage",
    r"\\clearpage",
    r"\\hss",
    r"\\ignorespaces",
    r"\\leavevmode",
    r"\\newpage",
    r"\\nobreak",
    r"\\pagebreak",
    r"\\penalty",
    r"\\ref",
    r"\\relax",
    r"\\samepage",
    r"\\sc",
    r"\\strut",
    r"\\textsc",
    r"\\vss",
    r"\\makebox(\[[^\]]*\])*\{[^{}]*\}",
    r"\{(?:\\mathchoice\{\}\{\}\{\}\{\})+\}",
    r"(?:\\mathchoice\{\}\{\}\{\}\{\})+",
]

CAPTION_PREFIXES = ["figure", "table", "listing"]


def _substitute_slash_in_latex(m: re.Match) -> str:
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
        the replaced string
    """
    return m.group(1) + "\\ " + m.group(3)


def _substitute_0pt(m: re.Match) -> str:
    """
    Handles cases where for some reason arxiv.html and ar5iv.html files convert the
    character 'n' to '0pt' while rendering. However there are genuine cases where
    '0pt' is present within '\\rule' directive of LaTeX which shouldnt be replaced
    with 'n'. In future if there are more such cases where the replacement should
    not happen, modify the regex in the below dict.

    Parameters
    ----------
    m : re.Match
        Each match object is passed iteratively to this callback method from
        re.sub call

    Returns
    -------
    str
        the replaced string
    """
    if m.group(1):
        return m.group(1).replace("0pt", "n")
    return m.group(0)


def _substitute_lambda(m: re.Match) -> str:
    """
    Handles substituting \\Lambda, \\varLambda, \\lambda with appropriate unicode
    symbols when present inside a \\textsf or \\text block

    Parameters:
    -----------
    m : re.Match
        Each match object is passed iteratively to this callback method from re.sub call

    Returns:
    -------
    str
        the replaced string
    """
    if m.group(1):
        return (
            m.group(1)
            .replace("\\Lambda", "Λ")
            .replace("\\varLambda", "Λ")
            .replace("\\varlambda", "Λ")
            .replace("\\lambda", "λ")
        )
    return m.group(0)


_REPLACE_LATEX_COMMANDS = {
    r"\\sans": r"\\textsf",
    r"\\mbox": r"\\text",
    r"(?m)(\{[^\\/]*)(\\/)(\})": _substitute_slash_in_latex,
    # very specific case for the paper https://arxiv.org/html/2310.17813 where
    # HTML itself is wrong and gives the string 0pt instead of n
    r"\\rule\{[^}]*\}\{[^}]*\}|(?<!\.)(0pt)": _substitute_0pt,
    # replace styling rules like \big, \bigg, \Big, \Bigg appropriately, by replacing
    # \big{(} to \big( i.e instead of character within parenthesis, it needs to be
    # just after the styling rule for KaTeX
    r"(\\[Bb]{1}i[g]{1,2}){(.)}": "\\1\\2",
    r"\\nicefrac": r"\\tfrac",
    r"\\arcsec": '"',
    r"\\arcmin": r"'",
    r"\\textmu": r"\\mu",
    r"\\text\{\\mu m\}": r"\\mu m",
    r"\\text(?:sf)?\{(\\(?:var)?(?:L|l)?ambda)\}": _substitute_lambda,
    r"\\begin\{array\}\[\]": r"\\begin{array}",
    r"\\Tr": r"\\operatorname{Tr}",
    r"\\imaginary": r"\\operatorname{Im}",
    r"\\\[[0-9]*(?:\.?[0-9]+)*pt\]": r"\\",
    r"\\vskip\s+\[?[0-9]*(?:\.?[0-9]+)?(?:pt|em)\]?": r"",
    r"\\farcs": r"\\prime\\prime.",
    # below 4 regexes strip extra '{' and '}' surrounding [A-Za-z0-9+-*] class eg. '{-a}'
    # this pattern is seen often after stripping commands, so we are replacing them with
    # the inner most capturing group as extra '{', '}' are redundant in LaTeX
    # eg '{{{+a}}}' replaced with '{+a}'
    r"\{{4}(\{[\-\+\*A-Za-z0-9]{1,5}\})\}{4}": "\\1",
    r"\{{3}(\{[\-\+\*A-Za-z0-9]{1,5}\})\}{3}": "\\1",
    r"\{{2}(\{[\-\+\*A-Za-z0-9]{1,5}\})\}{2}": "\\1",
    r"\{{1}(\{[\-\+\*A-Za-z0-9]{1,5}\})\}{1}": "\\1",
    r"\^\{\}": r"",
}

_FINAL_REPLACE_PATTERNS = {
    # cases where two inline latex expressions are one after the other and finally
    # get concatenated, they contain $ $ which is end of one expr and start of another,
    # however in our flow we are already surrounding the full expr by $$ $$ so we
    # need to remove such occurences within a single line.
    r"\$ \$": " ",
    # cases where a ltx_bold and ltx_italic text appear next to each other without
    # block level tags, in which case the pattern appears as *** in converted MD
    # which does not render as needed
    r"(?<!\\)\*{3}": "** *",
    r"\n{3,}": "\n\n",
}


def convert_html_to_markdown(
    html: str,
    *,
    remove_refs: bool = False,
    remove_toc: bool = False,
) -> str:
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
    convert_all_ltx_listing_to_md(soup)
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

    abstract_footnotes = []
    if abstract_tag:
        abstract_blocks, abstract_footnotes = _serialize_abstract(abstract_tag)
        blocks.extend(abstract_blocks)

    for tag in (title_tag, authors_tag, abstract_tag):
        if tag:
            tag.decompose()

    footnotes = []
    blocks.extend(_serialize_children(root, footnotes=footnotes))

    blk_cnt = _check_and_handle_latex_prefix_suffix(
        "\n\n".join(block for block in blocks if block).strip()
    )
    ftn_cnt = _check_and_handle_latex_prefix_suffix(
        "\n".join(footnote for footnote in (footnotes + abstract_footnotes)).strip()
    )
    return blk_cnt, ftn_cnt


def convert_fragment_to_markdown(
    html: str,
    *,
    remove_inline_citations: bool = False,
    base_url: str | None = None,
) -> tuple[str, str]:
    """
    Convert an HTML fragment into Markdown without title/author/abstract handling.

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

    Returns
    -------
    tuple[str, str]
        first entry is the markdown string
        second entry is the footnote string to be appended at the end
    """
    soup = BeautifulSoup(html, "html.parser")
    _strip_unwanted_elements(soup)
    convert_all_mathml_to_latex(soup)
    convert_all_ltx_listing_to_md(soup)
    fix_tabular_tables(soup)
    if base_url:
        _resolve_image_urls(soup, base_url)
    footnotes = []
    blocks = _serialize_children(
        soup, remove_inline_citations=remove_inline_citations, footnotes=footnotes
    )

    blk_cnt = _check_and_handle_latex_prefix_suffix(
        "\n\n".join(block for block in blocks if block).strip()
    )
    ftn_cnt = _check_and_handle_latex_prefix_suffix(
        "\n".join(footnote for footnote in footnotes).strip()
    )
    return blk_cnt, ftn_cnt


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
    for tag in soup.select(
        "button.sr-only, div.package-alerts, div.ltx_pagination, footer"
    ):
        tag.decompose()
    for tag in soup.select(
        ".ltx_note_content > .ltx_note_mark,.ltx_note_content > .ltx_tag_note"
    ):
        tag.decompose()


def _format_content(cont: str, indent: int = 2) -> str:
    try:
        cont = json.dumps(json.loads(cont), indent=indent)
    except json.JSONDecodeError:
        # ignore as there could be some ltx_listing content
        # which is not actually json content in which case,
        # return the original content as is.
        pass
    return cont


def convert_all_ltx_listing_to_md(soup: BeautifulSoup) -> None:
    for ltx_list_data in soup.select(".ltx_lstlisting > .ltx_listing_data"):
        ltx_list_data_a = ltx_list_data.find("a")
        if ltx_list_data_a:
            cont = ltx_list_data_a.get("href", "").split(",")
            if len(cont) == 2:
                cont = b64decode(cont[-1]).decode("utf-8")
            else:
                cont = ""

            pre_tag = soup.new_tag("pre")
            pre_tag["class"] = "ltx_listing_pre"
            pre_tag.string = f"```\n{_format_content(cont)}\n```"

            parent_node = ltx_list_data.parent
            parent_node.clear(decompose=True)
            parent_node.append(pre_tag)
        else:
            pass


def _check_and_handle_latex_prefix_suffix(md: str) -> str:
    parts = re.split(f"{_LATEX_PREFIX}|{_LATEX_SUFFIX}", md)
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

    for srch, repl in _FINAL_REPLACE_PATTERNS.items():
        fixed_md = re.sub(srch, repl, fixed_md)
    return fixed_md


def _correct_multiline_latex_handling(eqn_text: str) -> str:
    eqn_modified = ""

    def _detect_head_tail(eqn_text: str) -> tuple[str, str]:
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
        return head, tail

    def _remove_dollars(eqn_text: str) -> str:
        # remove terminal spaces and $'s as this will be added eventually
        eqn_text = eqn_text.strip().strip("$")

        # substitute all occurences of $ in the middle of equation text that are not
        # escaped with a \ symbol as in \$ retained, but standalone $ replaced
        eqn_text = re.sub(r"(?<!\\)\$", "", eqn_text)
        return eqn_text

    matches = _EQUATION_NUMBER_RE.findall(eqn_text)
    for match in matches:
        eqn_text, mid, post = match

        eqn_text = _remove_dollars(eqn_text)
        mid = mid.strip()
        post = post.strip()

        head, tail = _detect_head_tail(eqn_text)
        eqn_modified += (
            f"\n{head}\n{eqn_text} \\tag{{{mid.strip('()')}}}\n{tail}\n {post}\n"
        )

    if not eqn_modified:
        # case where the equation exists but does not contain a numbering
        # of the format (1), (2) etc.
        eqn_text = _remove_dollars(eqn_text)
        head, tail = _detect_head_tail(eqn_text)
        eqn_modified = f"{head}{eqn_text}{tail}"

    return eqn_modified


def _sanitize_latex_source(latex_source: str) -> str:
    def _strip_mathbf_within_text(ltx_src: str) -> str:
        if not "\\text{" in ltx_src:
            return ltx_src
        if not "\\mathbf{" in ltx_src:
            return ltx_src

        ltx_new_src = ""
        depth = -1
        is_replaced = 0
        for ch in ltx_src:
            ltx_new_src += ch
            if ltx_new_src.endswith("\\text{") or (depth > -1 and ch == "{"):
                depth = depth + 1

            if depth > -1 and ch == "}":
                depth = depth - 1
                if depth == 0 and is_replaced == 1:
                    ltx_new_src = ltx_new_src.replace("\\text{", "\\mathrm{")
                    is_replaced = 0

            if depth > -1:
                if ltx_new_src.endswith("\\mathbf"):
                    ltx_new_src = ltx_new_src.replace("\\mathbf", "")
                    is_replaced = 1

        return ltx_new_src

    def _strip_subscript_superscript_within_text(ltx_src: str) -> str:
        if "\\text{" in ltx_src:
            new_latex = ""
            count = 0
            for ch in ltx_src:
                new_latex += ch
                if new_latex.endswith("\\text{") or (
                    count > 0 and new_latex.endswith("{")
                ):
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
            ltx_src = re.sub(r"\\([_^])", r"\1", ltx_src)
            ltx_src = re.sub(r"\\(?=[\[\]])", "", ltx_src)
            return ltx_src

    def _modify_leq_within_text(ltx_src: str) -> str:
        if "\\text{" in ltx_src and "\\leq" in ltx_src:
            new_latex = ""
            count = 0
            for ch in ltx_src:
                new_latex += ch
                if new_latex.endswith("\\text{") or (
                    count > 0 and new_latex.endswith("{")
                ):
                    count += 1
                elif new_latex.endswith("}") and count > 0:
                    count -= 1
                elif new_latex.endswith("\\leq") and count > 0:
                    new_latex = new_latex[0:-4] + "≤"
            return new_latex
        else:
            return ltx_src

    def _strip_mathfrak_within_text(ltx_src: str) -> str:
        if not "\\text{" in ltx_src:
            return ltx_src
        if not "\\mathfrak{" in ltx_src:
            return ltx_src

        ltx_new_src = ""
        depth = -1
        for ch in ltx_src:
            ltx_new_src += ch
            if ltx_new_src.endswith("\\text{") or (depth > -1 and ch == "{"):
                depth = depth + 1

            if depth > -1 and ch == "}":
                depth = depth - 1

            if depth > -1:
                if ltx_new_src.endswith("\\mathfrak"):
                    ltx_new_src = ltx_new_src.replace("\\mathfrak", "")

        return ltx_new_src

    latex_source = re.sub(r"(?<!\\)%|\$", "", latex_source)
    for pattern in _STRIP_LATEX_COMMANDS:
        latex_source = re.sub(pattern, "", latex_source)

    for pattern, replacement in _REPLACE_LATEX_COMMANDS.items():
        latex_source = re.sub(pattern, replacement, latex_source)

    latex_source = _strip_mathbf_within_text(latex_source)
    latex_source = _strip_subscript_superscript_within_text(latex_source)
    latex_source = _modify_leq_within_text(latex_source)
    latex_source = _strip_mathfrak_within_text(latex_source)

    return latex_source


def escape_asterisks_outside_braces(s: str) -> str:
    """
    Escapes '*' with '\\*' only when it appears outside of '{' '}' pairs.
    Handles nested braces correctly via depth tracking.
    """
    result = []
    depth = 0

    for char in s:
        if char == "{":
            depth += 1
            result.append(char)
        elif char == "}":
            depth = max(0, depth - 1)  # guard against unmatched '}'
            result.append(char)
        elif char == "*" and depth == 0:
            result.append("\\*")  # outside braces — escape it
        else:
            result.append(char)  # inside braces or non-'*' — leave it

    return "".join(result)


def _normalize_pure_text_content(text: str) -> str:
    text = escape_asterisks_outside_braces(text)
    text = text.replace("\n", " ")
    text = text.replace("\xa0", " ")
    return text


def convert_all_mathml_to_latex(root: BeautifulSoup) -> None:
    for math in root.find_all("math"):
        annotation = math.find("annotation", attrs={"encoding": "application/x-tex"})
        if annotation and annotation.text:
            latex_source = annotation.text.strip()
            latex_source = _sanitize_latex_source(latex_source)
            math.replace_with(f"${_LATEX_PREFIX}{latex_source}{_LATEX_SUFFIX}$")
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


def _serialize_children(
    container: Tag, *, remove_inline_citations: bool = False, footnotes: list[str] = []
) -> list[str]:
    blocks: list[str] = []
    for child in container.children:
        if isinstance(child, NavigableString):
            continue
        if not isinstance(child, Tag):
            continue
        if (
            child.name == "div"
            and container.name == "div"
            and "span" in [grand_child.name for grand_child in child.children]
        ) or (
            child.name == "span"
            and container.name == "span"
            and (
                (
                    "ltx_para" in child.get("class", [])
                    and "ltx_theorem" in container.get("class", [])
                )
                or (
                    "ltx_minipage" in container.get("class", [])
                    and (
                        "ltx_para" in child.get("class", [])
                        or "ltx_p" in child.get("class", [])
                        or "ltx_itemize" in child.get("class", [])
                        or "ltx_listing" in child.get("class", [])
                    )
                )
                or (
                    "ltx_minipage" in container.parent.get("class", [])
                    and "ltx_quote" in container.get("class", [])
                    and "ltx_p" in child.get("class", [])
                )
            )
        ):
            # these checks are very specific cases where the NavigableString nodes
            # within _serialize_children get totally ignored due to which certain
            # content gets missed, these checks make it more robust to handle
            # such cases similar to para
            # the main issue is these are cases where a <div> is followed by nested
            # <span> tags whereas the general behavior is presence of <p> tags
            # which is what is properly handled by default
            # these checks handle specific edge cases came across during testing
            blocks.extend(
                [
                    _serialize_paragraph(
                        child,
                        remove_inline_citations=remove_inline_citations,
                        footnotes=footnotes,
                    )
                ]
            )
        blocks.extend(
            _serialize_block(
                child,
                remove_inline_citations=remove_inline_citations,
                footnotes=footnotes,
            )
        )
    return blocks


def _serialize_block(
    tag: Tag, *, remove_inline_citations: bool = False, footnotes: list[str] = []
) -> list[str]:
    if tag.name in {"section", "article", "div", "span"}:
        return _serialize_children(
            tag, remove_inline_citations=remove_inline_citations, footnotes=footnotes
        )

    if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag.name[1])
        heading = _normalize_text(tag.get_text(" ", strip=True))
        if not heading:
            return []
        return [f"{'#' * level} {heading}"]

    if tag.name == "pre" and "ltx_listing_pre" in tag.get("class", []):
        return [tag.string]

    if tag.name == "pre" and "ltx_font_typewriter" in tag.get("class", []):
        return [f"```\n{tag.string}\n```"]

    if tag.name == "p":
        paragraph = _serialize_paragraph(
            tag, remove_inline_citations=remove_inline_citations, footnotes=footnotes
        )
        return [paragraph] if paragraph else []

    if tag.name in {"ul", "ol"}:
        lines = _serialize_list(
            tag, remove_inline_citations=remove_inline_citations, footnotes=footnotes
        )
        return ["\n".join(lines)] if lines else []

    if tag.name == "figure":
        figure = _serialize_figure(
            tag, remove_inline_citations=remove_inline_citations, footnotes=footnotes
        )
        return [figure] if figure else []

    if tag.name == "table":
        table_md = _serialize_table(
            tag, remove_inline_citations=remove_inline_citations, footnotes=footnotes
        )
        return [table_md] if table_md else []

    if tag.name == "blockquote":
        content = _normalize_text(
            _serialize_inline(
                tag,
                remove_inline_citations=remove_inline_citations,
                footnotes=footnotes,
            )
        )
        if not content:
            return []
        return ["> " + content]

    if tag.name == "br":
        return []

    return _serialize_children(
        tag, remove_inline_citations=remove_inline_citations, footnotes=footnotes
    )


def _serialize_abstract(tag: Tag) -> tuple[list[str], list[str]]:
    blocks = ["## Abstract"]
    paragraphs = tag.find_all("p")
    if not paragraphs:
        content = _normalize_text(tag.get_text(" ", strip=True))
        if content:
            blocks.append(content)
        return blocks, []

    footnotes = []
    for paragraph in paragraphs:
        text = _serialize_paragraph(paragraph, footnotes=footnotes)
        if text:
            blocks.append(text)
    return blocks, footnotes


def _serialize_paragraph(
    tag: Tag,
    *,
    remove_inline_citations: bool = False,
    footnotes: list[str] = [],
    maintain_terminal_spaces: bool = False,
) -> str:
    content = _serialize_inline(
        tag, remove_inline_citations=remove_inline_citations, footnotes=footnotes
    )

    if maintain_terminal_spaces:
        content = _cleanup_non_terminal_spaces(content)
    else:
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
        child.name == "span"
        and "ltx_missing_citation" in child.get("class", [])
        and "ltx_ref_self" in child.get("class", [])
        for child in children
    )


def _is_internal_paper_link(href: str | None) -> bool:
    """Check if a link is an internal paper section reference (e.g., arxiv.org/html/...#S2.SS1)."""
    if not href:
        return False
    return "arxiv.org/html/" in href and "#" in href and "#bib" not in href


def _serialize_inline(
    node: Tag | NavigableString,
    *,
    remove_inline_citations: bool = False,
    indent: int = 0,
    nested_table: bool = False,
    footnotes: list[str] = [],
) -> str:
    if isinstance(node, NavigableString):
        return _normalize_pure_text_content(str(node))

    if "ltx_minipage" in node.get("class", []):
        # already handled via _serialize_children
        return ""

    if node.name == "br":
        return "\n"

    if "ltx_ERROR" in node.get("class", []):
        return ""

    if node.name == "pre" and "ltx_listing_pre" in node.get("class", []):
        # already would have been handled within _serialize_block
        #
        return ""

    if node.name in {"em", "i"} or "ltx_font_italic" in node.get("class", []):
        txt = _serialize_children_inline(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            footnotes=footnotes,
        ).strip()
        return f" *{txt}* " if txt else ""

    if "ltx_font_bold" in node.get("class", []) and "ltx_font_typewriter" in node.get(
        "class", []
    ):
        code_block = _serialize_children_inline(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            footnotes=footnotes,
        ).replace("\\*", "*")
        return f" **`{_format_content(code_block)}`** "

    if node.name in {"strong", "b"} or "ltx_font_bold" in node.get("class", []):
        txt = _serialize_children_inline(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            footnotes=footnotes,
        ).strip()
        return f"**{txt}** " if txt else ""

    if node.name == "a":
        text = _serialize_children_inline(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            footnotes=footnotes,
        ).strip()
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

    if node.name == "span" and "ltx_role_footnote" in node.get("class", []):
        text = _normalize_pure_text_content(
            node.select_one("sup").get_text(" ", strip=True)
        )
        footnotes.append(
            f"[^{text}]: {_normalize_pure_text_content(node.select_one('.ltx_note_outer').get_text(' ', strip=True))}  "
        )
        return f"[^{text}]"

    if "ltx_note_mark" in node.get("class", []) or "ltx_note_content" in node.get(
        "class", []
    ):
        # footnote already handled in the previous check when the parent tag is
        # processed, so skipping double processing here
        return ""

    if node.name == "sup":
        text = _serialize_children_inline(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            footnotes=footnotes,
        ).strip()
        return f"^{text}" if text else ""

    if node.name == "cite":
        if _is_invalid_citation(node):
            return ""

        if remove_inline_citations and "ltx_cite" in node.get("class", []):
            return ""
        return _serialize_children_inline(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            footnotes=footnotes,
        ).strip()

    if node.name == "math":
        text = node.get_text(" ", strip=True)
        return f"${text}$" if text else ""

    if "ltx_note" in node.get("class", []):
        text = _normalize_text(
            _serialize_children_inline(
                node,
                remove_inline_citations=remove_inline_citations,
                indent=indent,
                footnotes=footnotes,
            )
        )
        return f"({text})" if text else ""

    if "ltx_tag_item" in node.get("class", []):
        return ""

    if node.name in {"code"} or "ltx_font_typewriter" in node.get("class", []):
        code_block = _serialize_children_inline(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            footnotes=footnotes,
        ).replace("\\*", "*")
        code_block = _format_content(code_block)
        return f"`{code_block}`"

    if node.name in {"ul", "ol"}:
        lines = _serialize_list(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            footnotes=footnotes,
        )
        return "\n".join(lines) if lines else ""

    if node.name == "table" and not nested_table:
        table_md = _serialize_table(
            node, remove_inline_citations=remove_inline_citations, footnotes=footnotes
        )
        return table_md

    if "ltx_item" in node.get("class", []):
        item_text = _serialize_children_inline(
            node,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            nested_table=nested_table,
            footnotes=footnotes,
        )
        return item_text + "\n"

    return _serialize_children_inline(
        node,
        remove_inline_citations=remove_inline_citations,
        indent=indent,
        nested_table=nested_table,
        footnotes=footnotes,
    )


def _serialize_children_inline(
    tag: Tag,
    *,
    remove_inline_citations: bool = False,
    indent: int = 0,
    nested_table: bool = False,
    footnotes: list[str] = [],
) -> str:
    return "".join(
        _serialize_inline(
            child,
            remove_inline_citations=remove_inline_citations,
            indent=indent,
            nested_table=nested_table,
            footnotes=footnotes,
        )
        for child in tag.children
    )


def _cleanup_inline_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def _cleanup_eqn_text(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text


def _replace_terminal_spaces(s):
    # modifying the terminal " " character to the non-breaking unicode space character
    # so that these get rendered in markdown, in general the normal space characters are
    # not rendered by markdown renderers as they are considered superfluous
    NBSP = "\u00a0"
    s = re.sub(r"^ +", lambda m: NBSP * len(m.group()), s)
    s = re.sub(r" +$", lambda m: NBSP * len(m.group()), s)
    return s


def _cleanup_non_terminal_spaces(text: str) -> str:
    text = text.replace("\t\n", " ")
    text = re.sub(r"(?<=\S) +(?=\S)", " ", text)
    text = _replace_terminal_spaces(text)
    return text


def _serialize_list(
    list_tag: Tag,
    indent: int = 0,
    *,
    remove_inline_citations: bool = False,
    footnotes: list[str] = [],
) -> list[str]:
    lines: list[str] = []
    for item in list_tag.find_all("li", recursive=False):
        item_text_parts: list[str] = []
        nested_lists: list[Tag] = []
        for child in item.children:
            if isinstance(child, Tag) and child.name in {"ul", "ol"}:
                nested_lists.append(child)
            else:
                item_text_parts.append(
                    _serialize_inline(
                        child,
                        remove_inline_citations=remove_inline_citations,
                        indent=indent + 1,
                        footnotes=footnotes,
                    )
                )
        item_text = (
            _cleanup_inline_text("".join(item_text_parts))
            if indent
            else "".join(item_text_parts).strip()
        )
        prefix = "  " * indent + "- "
        lines.append(prefix + item_text if item_text else prefix.rstrip())
        for nested in nested_lists:
            lines.extend(
                _serialize_list(
                    nested,
                    indent + 1,
                    remove_inline_citations=remove_inline_citations,
                    footnotes=footnotes,
                )
            )
    return lines


def _serialize_toc(toc_nav: Tag) -> str:
    list_tag = toc_nav.find("ol")
    if not list_tag:
        return ""
    lines = _serialize_list(list_tag, footnotes="")
    return "\n".join(lines)


def _update_rowspan_info(
    rowspan_info: dict, values: list[str], row_idx: int
) -> list[str]:
    if rowspan_info:
        for col_idx, row_span in rowspan_info.items():
            for row_start, row_end in row_span:
                if row_idx > row_start and row_idx < row_end:
                    values.insert(col_idx, "")
    return values


def _serialize_eqn_table(table: Tag) -> str:
    tbody_list = table.find_all("tbody", recursive=False)

    if not tbody_list:
        return _correct_multiline_latex_handling(
            _normalize_text(table.get_text(" ", strip=True))
        )

    accumulated_val = ""
    for tbody in tbody_list:
        txt = _correct_multiline_latex_handling(
            _normalize_text(tbody.get_text(" ", strip=True))
        )

        # ensure the methods _normalize_text and  _correct_multiline_latex_handling
        # are called before creating the \n reformatting and not after it is done.
        # the new line injection is wanted as this handles multiple tbody math equations
        # that are present within one table tag.
        # if we reorder, the method _normalize_text replaces all new lines with spaces
        # nullyfying the effect of adding \n here
        accumulated_val += f"{txt}\n"
    return _cleanup_eqn_text(accumulated_val)


def _serialize_table(
    table: Tag, *, remove_inline_citations: bool = False, footnotes: list[str] = []
) -> str:
    classes = " ".join(table.get("class", []))
    if _EQUATION_TABLE_RE.search(classes):
        eqn_text = _serialize_eqn_table(table)
        return eqn_text or ""

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

                    cell_text = _cleanup_inline_text(
                        _serialize_inline(
                            cell,
                            remove_inline_citations=remove_inline_citations,
                            nested_table=True,
                            footnotes=footnotes,
                        )
                    ).replace("\n", "<br>")
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

                cell_text = _cleanup_inline_text(
                    _serialize_inline(
                        cell,
                        remove_inline_citations=remove_inline_citations,
                        nested_table=True,
                        footnotes=footnotes,
                    )
                ).replace("\n", "<br>")
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


def _serialize_span_table(
    table: Tag, *, remove_inline_citations: bool = False, footnotes: list[str] = []
) -> str:
    classes = " ".join(table.get("class", []))
    if _EQUATION_TABLE_RE.search(classes):
        eqn_text = _serialize_eqn_table(table)
        return eqn_text or ""

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

                    cell_text = _cleanup_inline_text(
                        _serialize_inline(
                            cell,
                            remove_inline_citations=remove_inline_citations,
                            nested_table=True,
                            footnotes=footnotes,
                        )
                    ).replace("\n", "<br>")
                    values.append(cell_text)

                values = _update_rowspan_info(rowspan_info, values, row_idx)
                rows.append(values)
    else:
        # Table has no tbody/thead/tfoot - find rows directly in table
        for row_idx, row in enumerate(
            table.find_all("span", attrs={"class": "ltx_tr"}, recursive=False)
        ):
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

                cell_text = _cleanup_inline_text(
                    _serialize_inline(
                        cell,
                        remove_inline_citations=remove_inline_citations,
                        nested_table=True,
                        footnotes=footnotes,
                    )
                ).replace("\n", "<br>")
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


def _minipage_figure(
    minipage_span: Tag,
    *,
    remove_inline_citations: bool = False,
    footnotes: list[str] = [],
) -> str:
    lines = []
    lines.extend(
        _serialize_children(
            minipage_span,
            remove_inline_citations=remove_inline_citations,
            footnotes=footnotes,
        )
    )
    return "\n".join(lines).strip()


def _check_and_return_caption(
    caption: str, prefix_check: str | list[str] = "", prefix_add: str = ""
) -> str:
    if not prefix_check and not prefix_add:
        return caption

    if type(prefix_check) == str:
        prefix_check = [prefix_check]

    if any([caption.lower().startswith(prefix) for prefix in prefix_check]):
        return caption
    else:
        return f"{prefix_add}{caption}"


def _serialize_figure(
    figure: Tag, *, remove_inline_citations: bool = False, footnotes: list[str] = []
) -> str:
    # Check if this is a table figure (ltx_table class)
    figure_classes = " ".join(figure.get("class", []))
    recursive_figures = figure.find_all("figure")
    is_table_figure = "ltx_table" in figure_classes
    is_algorithm_figure = (
        "ltx_framed" in figure_classes or "ltx_algorithm" in figure_classes
    )
    is_ltx_listing_figure = "ltx_lstlisting" in figure_classes
    minipage_figures = [
        tag
        for tag in figure.select("span span.ltx_minipage")
        if not tag.find_parent(class_="ltx_minipage")
    ]

    caption_tags = [
        fig_caption
        for fig_caption in figure.find_all("figcaption")
        if fig_caption.find_parent("figure") is figure
    ]
    caption = (
        _normalize_text(
            _serialize_inline(
                caption_tags[0] if not recursive_figures else caption_tags[-1],
                remove_inline_citations=remove_inline_citations,
                footnotes=footnotes,
            )
        )
        # case when the overall figure tag does not have a fig caption, this check
        # prevents the last figure's caption from being repeated as the overall figure's
        # caption, if number of captions match number of figures, then each caption is
        # added for each figure, else need to add the caption
        # three cases:
        # 1. overall caption + caption for each figure within a figure tag
        # 2. only caption for each figure available though present within an overall figure tag
        # 3. recursive figure tag present but only one figure within top level recursive figure tag
        # There are some failure modes still, but they become too specific to handle
        if (
            caption_tags
            and (len(caption_tags) == 1 or len(caption_tags) != len(recursive_figures))
        )
        else ""
    )

    lines = []

    if is_table_figure:
        # Handle table figures - find and serialize the embedded table
        # Note: fix_tabular_tables strips attributes, so search for any table element
        table = figure.find("table")
        if table:
            table_md = _serialize_table(
                table,
                remove_inline_citations=remove_inline_citations,
                footnotes=footnotes,
            )
            if caption:
                lines.append(f"**{caption}**")
            if table_md:
                lines.append(table_md)
        elif minipage_figures:
            for minipage_figure in minipage_figures:
                lines.append(_minipage_figure(minipage_figure))

            if caption:
                lines.append(
                    _check_and_return_caption(caption, CAPTION_PREFIXES, "Figure: ")
                )
        elif caption:
            span_table = figure.find("span", attrs={"class": "ltx_tabular"})
            if span_table:
                span_table_md = _serialize_span_table(
                    span_table,
                    remove_inline_citations=remove_inline_citations,
                    footnotes=footnotes,
                )
                if caption:
                    lines.append(f"**{caption}**")
                if span_table_md:
                    lines.append(span_table_md)
            else:
                # Fallback if no table found but has caption
                lines.append(
                    f"Table: {caption}"
                    if not caption.lower().startswith("table")
                    else f"{caption}"
                )
    elif is_algorithm_figure:
        div = figure.find("div")
        span = figure.select_one("span span.ltx_minipage")
        if div:
            inner_line_divs = div.find_all("div")
        elif span:
            inner_minipage_figs = span.parent.select("span.ltx_minipage")

        lines.append(f">{caption}")
        lines.append(f">---  ")
        if div:
            lines.extend(
                [
                    f">{_serialize_paragraph(inner_line_div, remove_inline_citations=remove_inline_citations, footnotes=footnotes, maintain_terminal_spaces=True)}  "
                    for inner_line_div in inner_line_divs
                ]
            )
        else:
            for minipage_figure in inner_minipage_figs:
                lines.extend(
                    list(
                        map(
                            lambda s: f">{s}",
                            re.split(
                                r"\u2004\u200a|\u2003\u2004|\n",
                                _minipage_figure(minipage_figure),
                            ),
                        )
                    )
                )
    elif minipage_figures:
        for minipage_figure in minipage_figures:
            lines.append(_minipage_figure(minipage_figure))

        if caption:
            lines.append(
                _check_and_return_caption(caption, CAPTION_PREFIXES, "Figure: ")
            )
    elif is_ltx_listing_figure:
        lines.extend(_serialize_children(figure.find("div", class_="ltx_listing")))
        if caption:
            lines.append(
                _check_and_return_caption(caption, CAPTION_PREFIXES, "Figure: ")
            )
    else:
        if recursive_figures:
            for fig in recursive_figures:
                lines.append(
                    _serialize_figure(
                        fig,
                        remove_inline_citations=remove_inline_citations,
                        footnotes=footnotes,
                    )
                )

        if not recursive_figures:
            # Handle regular image figures
            imgs = figure.find_all("img")
            if imgs:
                for img in imgs:
                    src = img.get("src") if img else None
                    alt = img.get("alt") if img else None
                    if src:
                        image_label = alt or "Image"
                        lines.append(f"![{image_label}]({src})  ")

        if caption:
            # when ltx_figure_panel is in classes it is generally a nested list of figures
            # so the Figure prefix will be given only for the outermost caption
            if "ltx_figure_panel" in figure_classes:
                lines.append(f"{caption}")
            else:
                lines.append(
                    _check_and_return_caption(caption, CAPTION_PREFIXES, "Figure: ")
                )

    return (
        "\n".join(lines).strip()
        if not recursive_figures
        else "\n\n".join(lines).strip()
    )


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
