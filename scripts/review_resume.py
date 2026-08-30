"""`make review-resume` — score a résumé against a posting and say what to fix.

    python -m scripts.review_resume --resume path/to/resume.pdf --posting-file jd.txt
    python -m scripts.review_resume --resume r.docx --golden "Forward Deployed"

Reads the résumé the same way the pipeline does, so what it reports is what the
pipeline sees — including a parse that went wrong, which is worth finding here
rather than after an employer received the file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packages.tailor.parse import parse_resume, parse_text
from packages.tailor.report import build, render

GOLDEN = Path("tests/fixtures/golden/postings.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path, required=True, help="pdf, docx, txt or md")
    parser.add_argument("--posting-file", type=Path, help="the job description as text")
    parser.add_argument("--golden", help="instead, match a posting title in the crawled golden set")
    args = parser.parse_args(argv)

    if not args.resume.is_file():
        print(f"no such résumé: {args.resume}", file=sys.stderr)
        return 2

    if args.resume.suffix.lower() in {".txt", ".md"}:
        resume = parse_text(args.resume.read_text(encoding="utf-8"))
    else:
        resume = parse_resume(args.resume.read_bytes(), args.resume.name)

    if args.posting_file:
        posting_text = args.posting_file.read_text(encoding="utf-8")
        title = args.posting_file.name
    elif args.golden:
        if not GOLDEN.is_file():
            print(f"missing golden set: {GOLDEN}", file=sys.stderr)
            return 2
        matches = [
            p
            for p in json.loads(GOLDEN.read_text())["postings"]
            if args.golden.lower() in p["title"].lower()
        ]
        if not matches:
            print(f"no golden posting matching {args.golden!r}", file=sys.stderr)
            return 2
        posting_text = matches[0]["description"]
        title = f"{matches[0]['title']} — {matches[0]['company']}"
    else:
        print("give --posting-file or --golden", file=sys.stderr)
        return 2

    print(f"=== {title} ===")
    print(
        f"résumé: {args.resume}  ({len(resume.raw_lines)} lines, "
        f"sections {sorted(resume.sections)})"
    )
    print()
    print(render(build(resume, posting_text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
