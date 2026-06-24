import sys

import arxiv2md


def run_conv(arxiv_id: str):
    cnt = arxiv2md.ingest_paper_sync(arxiv_id, remove_inline_citations=False).content
    with open(f"./outputs/{arxiv_id}.md", "w") as fp:
        fp.write(cnt)


if len(sys.argv) > 1:
    arxiv_id = sys.argv[1]
else:
    arxiv_id = "2603.07685"

run_conv(arxiv_id)
