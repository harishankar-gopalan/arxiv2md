"""
This module helps in re-ordering specific top level constructs, namely:
- ltx_section
- ltx_subsection
- ltx_subsubsection
- ltx_appendix

This re-ordering is mainly done to pull out sections to the top-level in HTML papers
where sections are nested into one other. Though the HTML rendering is fine, the TOC
of such papers in the HTML rendering is wrong due to invalid nested rendering.

The re-ordering is performed solely based on the 'id' value assigned to each of them.
For example sections are numbered as S.1, S.2, Sx1, Sx2, S.3 and so on. For sections
with plain number post the '.' they are ordered in the number order. For sections that
are extra denoted by 'x' after the '.' they are maintained in the same order as they
are present in the HTML content.

Same logic is applied for appendices, subsection and subsubsection with the difference
in their 'id' conventions, like subsections are number S<sec-id>.SS<subsec-id>.

Note: Major part of this module is generated using Claude, however comments are added
      post analyzing/understanding the generated code.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

from bs4 import BeautifulSoup, Tag

LEVELS = (
    "ltx_section",
    "ltx_appendix",
    "ltx_subsection",
    "ltx_subsubsection",
    "ltx_paragraph",
)

SHORT = {
    "ltx_section": "section",
    "ltx_appendix": "appendix",
    "ltx_subsection": "subsection",
    "ltx_subsubsection": "subsubsection",
    "ltx_paragraph": "paragraph",
}

RANK = {
    "ltx_section": 0,
    "ltx_appendix": 0,
    "ltx_subsection": 1,
    "ltx_subsubsection": 2,
    "ltx_paragraph": 3,
}

ID_RE = re.compile(
    r"^(?P<kind>[SA])(?P<topx>x?)(?P<top>\d+)"  # regex for handling top-level section and appendix, hence [SA]
    r"(?:\.SS(?P<ssx>x?)(?P<ss>\d+))?"  # regex for handling subsections
    r"(?:\.SSS(?P<sssx>x?)(?P<sss>\d+))?"  # regex for handling sub-subsections
    r"(?:\.P(?P<px>x?)(?P<p>\d+))?$"  # regex for handling paragraphs
)


# --------------------------------------------------------------------------
# id helpers
# --------------------------------------------------------------------------


def parse_id(
    el_id: str,
) -> tuple[int, tuple[int, ...], str, bool]:
    """A utility function to parse the 'id' string of any of the top-level html sections

    Args:
      el_id (str): the 'id' attribute value to be parsed

    Returns:
      tuple[int, tuple[int, ...], str, bool]:
        - int        : gives the rank of the element, as 0, 1, 2 based on whether it is a
                       section / appendix, a subsection or a sub-subsection
        - tuple[int, int, int] |
          tuple[int, int]      |
          tuple[int] : a tuple with either one, two or three values depending on whether
                       the input is a section / appendix, subsection or a sub-subsection
        - str        : contains 'S' or 'A' based on whether the top-level is a section
                       or appendix
        - bool       : returns whether the elements rank contains an 'x' in the id name
                       denoting its an extra section or subsection
    """
    m = ID_RE.match(el_id or "")
    if not m:
        return None
    d = m.groupdict()
    nums = [int(d["top"])]
    if d["ss"] is not None:
        nums.append(int(d["ss"]))
    if d["sss"] is not None:
        nums.append(int(d["sss"]))
    if d["p"] is not None:
        nums.append(int(d["p"]))
    if d["sss"] is not None and d["ss"] is None:  # SSS without SS
        return None
    if d["p"] is not None and d["sss"] is None:  # P without SSS
        return None
    # x-flag of the node's *own* (deepest) segment -- that is the one the
    # sibling sort compares on.  'Sx1' and 'S1.SSx2' are x-numbered;
    # 'Sx1.SS1' is not, its own SS segment is a regular number.
    if d["p"] is not None:
        own_x = bool(d["px"])
    elif d["sss"] is not None:
        own_x = bool(d["sssx"])
    elif d["ss"] is not None:
        own_x = bool(d["ssx"])
    else:
        own_x = bool(d["topx"])
    return len(nums) - 1, tuple(nums), d["kind"], own_x


def parent_id(el_id: str) -> str | None:
    """'S1.SS2.SSS3' -> 'S1.SS2';  'S1' -> None."""
    head, sep, _ = el_id.rpartition(".")
    return head if sep else None


def resolve_parent_id(el_id: str, by_id: dict[str, Any]) -> str | None:
    """The nearest *existing* ancestor id, skipping placeholder segments.

    LaTeXML numbers a paragraph that has no enclosing sub-subsection as
    'S3.SS2.SSS0.Px1' -- the 'SSS0' segment is a placeholder and no element
    carries that id (likewise 'SS0' for a paragraph directly under a section).
    Stripping one segment therefore yields a parent that does not exist, so we
    keep stripping until we find one that does.

    Returns None if no ancestor id is present in the document.
    """
    pid = parent_id(el_id)
    while pid is not None and pid not in by_id:
        pid = parent_id(pid)
    return pid


def node_level(tag: Tag) -> str | None:
    """The level class carried by `tag`, or None."""
    if not getattr(tag, "name", None):
        return None

    classes = tag.get("class") or []

    if isinstance(classes, str):
        classes = classes.split()
    for lvl in LEVELS:
        if lvl in classes:
            return lvl
    return None


def _nearest_level_ancestor(tag: Tag) -> Tag:
    """The nearest ancestor element that is of any of the required levels"""
    for p in tag.parents:
        if node_level(p) is not None:
            return p
    return None


def _outermost_level_ancestor(tag: Tag) -> Tag:
    """The outermost level element containing `tag` (or `tag` itself)."""
    out = tag
    for p in tag.parents:
        if node_level(p) is not None:
            out = p
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def absorb_sectional_blocks(
    soup: BeautifulSoup, block_cls: str = "ltx_sectional-block"
) -> BeautifulSoup:
    """Reunite a LaTeXML `\\section*` with its body.

    LaTeXML emits a starred section that appears mid-paragraph as an almost
    empty section inside a .ltx_sectional-block, with the actual body left as
    a *sibling* of that block.  Pull those siblings into the section so the
    body travels with the heading when the section is moved.

    No-op on documents that have no .ltx_sectional-block.

    This function is not used for now.
    """
    for block in soup.find_all(
        lambda t: node_level(t) is None and _has_class(t, block_cls)
    ):
        sec = block.find(lambda t: node_level(t) is not None)
        if sec is None:
            continue
        for sib in list(block.next_siblings):
            if getattr(sib, "name", None) is not None and (
                node_level(sib) is not None or _has_class(sib, block_cls)
            ):
                break
            sec.append(sib.extract())
    return soup


def _has_class(tag: Tag, cls: str) -> bool:
    """Utility to check if a class name is present in the tag's class list"""
    if not getattr(tag, "name", None):
        return False
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    return cls in classes


def rearrange(soup: BeautifulSoup, cleanup: bool = True) -> dict[str, Any]:
    """Rebuild the section hierarchy in place. Returns a report dict.

    Args:
      soup (BeautifulSoup): the root bs4 object to run rearrangement of nodes
      cleanup (bool, optional): removes empty elements if Ture. Defaults to True.

    Returns:
      dict[str, Any]: a debugging report containing the keys 'placed', 'orphans',
                      'unparsed', 'rank_mismatch', 'duplicated_ids'
    """
    nodes = soup.find_all(lambda t: node_level(t) is not None)
    if not nodes:
        return {
            "placed": 0,
            "orphans": [],
            "unparsed": [],
            "rank_mismatch": [],
            "duplicate_ids": [],
        }
    report = {
        "placed": 0,
        "orphans": [],
        "unparsed": [],
        "rank_mismatch": [],
        "duplicate_ids": [],
    }
    # --- 1. snapshot everything we need BEFORE mutating the tree ----------
    recs = []
    for i, tag in enumerate(nodes):
        cls = node_level(tag)
        eid = tag.get("id")
        parsed = parse_id(eid)
        anc = _nearest_level_ancestor(tag)
        rec = {
            "tag": tag,
            "id": eid,
            "cls": cls,
            "doc": i,
            "rank": parsed[0] if parsed else RANK[cls],
            "key": parsed[1] if parsed else None,
            "kind": parsed[2] if parsed else None,
            "xpos": parsed[3] if parsed else False,
            "dom_parent": anc,
            "top_tag": _outermost_level_ancestor(tag),
            "children": [],
        }
        if parsed is None:
            report["unparsed"].append(eid)
        elif parsed[0] != RANK[cls]:
            # id and class disagree about depth; id wins, but say so
            report["rank_mismatch"].append((eid, cls))
        recs.append(rec)

    by_id: dict[str, dict[str, Any]] = {}  # key = tag's id string, val = record dict
    for r in recs:
        if r["id"] is None:
            continue
        if r["id"] in by_id:
            report["duplicate_ids"].append(r["id"])
            continue
        by_id[r["id"]] = r
    by_tag: dict[int, dict[str, Any]] = {id(r["tag"]): r for r in recs}

    # anchors: one marker per *original* top-level node, so unrelated content
    # sitting between top-level sections (a bibliography, front/back matter)
    # keeps its place instead of being pushed behind the whole forest.
    first = nodes[0]
    container = first.parent
    markers = {}
    for r in recs:
        top = r["top_tag"]
        if id(top) not in markers:
            mk = soup.new_tag("div")
            mk["data-ltx-marker"] = "1"
            top.insert_before(mk)
            markers[id(top)] = mk
    marker = markers[id(recs[0]["top_tag"])]

    # --- 2. detach every level node, deepest first ------------------------
    # Reversed document order puts descendants before ancestors, so each node
    # comes out holding only its own non-level content.
    for tag in reversed(nodes):
        tag.extract()

    # --- 3. re-link by id, falling back to original DOM ancestry ----------
    roots: list[dict[str, Any]] = []  # contains all top-level element records dict
    for r in recs:
        pid = resolve_parent_id(r["id"], by_id) if r["id"] else None
        parent = None
        if r["key"] is not None and r["rank"] > 0:
            parent = by_id.get(pid)
            if parent is None:
                report["orphans"].append((r["id"], parent_id(r["id"])))
        elif r["key"] is None and r["dom_parent"] is not None:
            parent = by_tag.get(id(r["dom_parent"]))
        if parent is None:
            roots.append(r)
        else:
            parent["children"].append(r)

    # --- 4. sort siblings ------------------------------------------------
    def anchor_x(rec_list):
        """Borrow a sort number for SSx siblings from their doc-order neighbour.

        Their numbering is a separate series from SS, so it can't be compared
        against it. Instead an SSx node takes the number of the nearest
        regularly-numbered sibling that precedes it in the source document
        (or follows it, if it comes first).  Ties then break on 'doc', so it
        lands immediately after that neighbour -- or immediately before, when
        anchored forwards -- keeping its original neighbours.
        """
        ordered = sorted(rec_list, key=lambda r: r["doc"])
        for i, r in enumerate(ordered):
            if not r["xpos"] or r["key"] is None:
                continue
            num = None
            for prev in reversed(ordered[:i]):
                if prev["key"] is not None and not prev["xpos"]:
                    num = prev["key"][-1]
                    break
            if num is None:
                for nxt in ordered[i + 1 :]:
                    if nxt["key"] is not None and not nxt["xpos"]:
                        num = nxt["key"][-1]
                        break
            r["xnum"] = num if num is not None else r["key"][-1]
        return rec_list

    def sort_key(r):
        if r["key"] is None:
            return (1, 0, 0, r["doc"])  # unparsed: keep doc order, last
        kind = 0 if r["kind"] == "S" else 1  # sections before appendices
        num = r["xnum"] if r["xpos"] and "xnum" in r else r["key"][-1]
        return (0, kind, num, r["doc"])

    def attach(rec_list, parent_tag):
        for r in sorted(anchor_x(rec_list), key=sort_key):
            parent_tag.append(r["tag"])
            report["placed"] += 1
            attach(r["children"], r["tag"])

    # each root goes back at the slot its original top-level section occupied
    for r in sorted(anchor_x(roots), key=sort_key):
        mk = markers.get(id(r["top_tag"]), marker)
        mk.insert_before(r["tag"])
        report["placed"] += 1
        attach(r["children"], r["tag"])
    for mk in markers.values():
        mk.extract()
    if cleanup:
        drop_empty_wrappers(container if container is not None else soup)
    return report


def rearrange_if_needed(
    soup: BeautifulSoup, cleanup: bool = True
) -> dict[str, Any] | None:
    """Calls the 'rearrange' method if any of the section(s) are nested

    Args:
      soup (BeautifulSoup): the root bs4 object to run rearrangement of nodes
      cleanup (bool, optional): removes empty elements if Ture. Defaults to True.

    Returns:
      dict[str, Any] | None: a debugging report containing the keys 'placed', 'orphans',
                             'unparsed', 'rank_mismatch', 'duplicated_ids' if 'rearrange'
                             is called
    """
    rep = None
    if (
        soup.select(".ltx_section .ltx_section")
        or soup.select(".ltx_subsection .ltx_subsection")
        or soup.select(".ltx_subsubsection .ltx_subsubsection")
        or soup.select(".ltx_appendix .ltx_appendix")
        or soup.select(".ltx_paragraph .ltx_paragraph")
    ):
        rep = rearrange(soup, cleanup)
    return rep


def drop_empty_wrappers(
    root: BeautifulSoup,
    tags: set[str] = ("div", "section", "span"),
    keep: set[str] = ("img", "table", "svg", "math", "video", "br"),
) -> BeautifulSoup:
    """Remove wrappers left behind holding nothing."""
    for el in root.find_all(list(tags)):
        if node_level(el) is not None:
            continue
        if not el.get_text(strip=True) and not el.find(list(keep)):
            el.decompose()
    return root


def build_tree(root: BeautifulSoup) -> list[dict[str, Any]]:
    """Nested list of {'tag','id','level','children'} for matched nodes only."""

    def children_of(node):
        out = []
        for child in getattr(node, "contents", []):
            if getattr(child, "name", None) is None:
                continue
            lvl = node_level(child)
            if lvl is None:  # wrapper: look straight through
                out.extend(children_of(child))
            else:
                out.append(
                    {
                        "tag": child,
                        "id": child.get("id"),
                        "level": lvl,
                        "children": children_of(child),
                    }
                )
        return out

    lvl = node_level(root)
    if lvl is not None:
        return [
            {
                "tag": root,
                "id": root.get("id"),
                "level": lvl,
                "children": children_of(root),
            }
        ]
    return children_of(root)


def print_tree(soup: BeautifulSoup, show_level: bool = True, stream=None):
    """Print the hierarchy as an indented id tree."""

    stream = stream or sys.stdout
    forest = build_tree(soup)

    def render(nodes, prefix=""):
        for i, n in enumerate(nodes):
            last = i == len(nodes) - 1
            branch = "`-- " if last else "|-- "
            label = n["id"] or "<no id>"
            if show_level:
                label += f"  [{SHORT[n['level']]}]"
            print(prefix + branch + label, file=stream)
            render(n["children"], prefix + ("    " if last else "|   "))

    render(forest)
