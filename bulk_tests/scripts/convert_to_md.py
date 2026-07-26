import sys
from pathlib import Path

import arxiv2md

base_folder = Path(__file__).resolve().parent.parent


def run_conv(arxiv_id: str):
    cnt = arxiv2md.ingest_paper_sync(arxiv_id, remove_inline_citations=False).content
    with open(f"{base_folder}/outputs/{arxiv_id}.md", "w") as fp:
        fp.write(cnt)


if len(sys.argv) > 1:
    arxiv_id = sys.argv[1]
else:
    arxiv_id = "2603.07685"

run_conv(arxiv_id)
