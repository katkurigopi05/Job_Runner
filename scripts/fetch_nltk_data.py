"""Download the POS tagger the fabrication guard uses.

Separate from `make install` because this is data, not a package — pip cannot
carry it. Without it `packages/tailor/chunk.py` reports unavailable and the
guard falls back to matching on capitalization, which cannot see a lowercase
claim like "machine learning".

The fallback is deliberate and visible rather than fatal: a fresh machine
should be able to run the suite before it has fetched anything. What it must
not do is fall back *quietly*, so every `GuardReport` carries which extractor
produced it and `make doctor` checks for the data.
"""

from __future__ import annotations

import sys

#: `punkt` splits sentences, the tagger tags them. Both are needed and neither
#: ships with the wheel. The `_eng` variants are what recent NLTK looks for.
REQUIRED = (
    "punkt",
    "punkt_tab",
    "averaged_perceptron_tagger",
    "averaged_perceptron_tagger_eng",
)


def main() -> int:
    import nltk

    failed: list[str] = []
    for package in REQUIRED:
        if not nltk.download(package, quiet=True):
            failed.append(package)
        else:
            print(f"  ok  {package}")

    if failed:
        print(f"\ncould not fetch: {', '.join(failed)}", file=sys.stderr)
        print(
            "The guard will fall back to capitalization matching, which cannot\n"
            "see lowercase claims. GuardReport.extractor records this.",
            file=sys.stderr,
        )
        return 1

    from packages.tailor.chunk import available, noun_phrases

    if not available():
        print("data fetched but the chunker still will not load", file=sys.stderr)
        return 1

    print(f"\nnoun-phrase extraction is live: {noun_phrases('a message queue at scale')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
