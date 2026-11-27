"""Write the `--inputs` that `cases.jsonl` was recorded from.

Real lines, the ones `requirements.extract` hands `find_skills`, against every
pattern of `SKILLS` and `CODE_SKILLS` and the topic words `eligibility` looks
for: where `re` finds it, near misses (found once case and lookarounds are
ignored, which is what a port gets wrong), lines that name nothing, and traps
built from the finds. A trap swaps in the characters Python's `re.IGNORECASE`
pairs with an ASCII letter, Python-only whitespace, or a character either side
of the find. Each line is cut to a window around its find, which keeps every
lookaround and keeps the file small.

From the repository root, with the project's Python first on PATH:

    PYTHONPATH=. python rust/pattern-search-rs/inputs/make_inputs.py \\
        storage/qwen_batches.nosync/colab_2026-11-25/pending.json.gz /tmp/vocab.jsonl
    cat /tmp/vocab.jsonl rust/pattern-search-rs/inputs/syntax.jsonl > /tmp/inputs.jsonl
    rust-anything record rust/pattern-search-rs --inputs /tmp/inputs.jsonl

The export is a Qwen batch of chunked postings (`make export-chunk-vectors-pending`).
"""

from __future__ import annotations

import gzip
import json
import random
import re
import sys
from collections.abc import Sequence

from packages.matching import chunks, eligibility, requirements
from packages.matching.pattern_set import inline_pattern
from packages.matching.skill_vocab import CODE_SKILLS, SKILLS, Skill

STRIDE = chunks.CHUNK_SIZE - chunks.OVERLAP
PER_KIND = 20
TRAP_SOURCES = 5
#: Characters kept either side of a find: past any lookaround, short enough to commit.
MARGIN = 48
NEGATIVE_CHARS = 160
PARTNERS = {
    "i": "\u0130\u0131",
    "I": "\u0130\u0131",
    "k": "\u212a",
    "K": "\u212a",
    "s": "\u017f",
    "S": "\u017f",
}
ODD_SPACES = ["\x1c", "\x1f", "\xa0", "\u2003", "\u3000", "\x85", "\u200b", "\ufeff"]
NEIGHBOURS = [
    *("a", "Z", "7", "+", "#", ".", ".x", ".7", "_", "-"),
    *("\u00e9", "\u212a", "\u017f", "\u0130", "\u0131"),
]
#: A lookaround, with a class or one nested group inside. The alternatives are disjoint
#: (`[` only opens a class), so no input backtracks exponentially.
LOOKAROUND = re.compile(
    r"\(\?<?[=!]"
    r"(?:[^()\\\[]|\\.|\[[^\]]*\]|\((?:[^()\\\[]|\\.|\[[^\]]*\])*\))*"
    r"\)"
)
SEED = 20261126


def corpus_lines(path: str) -> list[str]:
    """Every distinct line `find_skills` is asked about while reading the export."""
    with gzip.open(path) as export:
        data = json.load(export)
    texts = [
        "".join(c[:STRIDE] for c in p["chunks"][:-1]) + p["chunks"][-1]
        for p in data["postings"]
        if p["chunks"]
    ]
    seen: dict[str, None] = {}
    original = requirements.find_skills

    def spy(line: str, vocabulary: Sequence[Skill] = SKILLS) -> list[Skill]:
        seen.setdefault(line, None)
        return original(line, vocabulary)

    requirements.find_skills = spy
    try:
        for text in texts:
            requirements.extract(text)
    finally:
        requirements.find_skills = original
    return list(seen)


def window(pattern: re.Pattern[str], line: str) -> str:
    found = pattern.search(line)
    if found is None:
        return line[:NEGATIVE_CHARS]
    return line[max(0, found.start() - MARGIN) : found.end() + MARGIN]


def traps(pattern: str, line: str) -> list[str]:
    found = re.search(pattern, line)
    if found is None:
        return []
    lo, hi = found.span()
    out = []
    for at in range(lo, hi):
        for partner in PARTNERS.get(line[at], ""):
            out.append(line[:at] + partner + line[at + 1 :])
        if line[at] == " ":
            out.extend(line[:at] + space + line[at + 1 :] for space in ODD_SPACES)
    for neighbour in NEIGHBOURS:
        out.append(line[:lo] + neighbour + line[lo:])
        out.append(line[:hi] + neighbour + line[hi:])
    out.extend((line[lo:hi], line[lo:hi].upper(), line[lo:hi].lower()))
    return out


def cases_for(pattern: str, lines: list[str], rng: random.Random) -> list[tuple[str, str]]:
    exact = re.compile(pattern)
    loose = re.compile(LOOKAROUND.sub("", pattern.removeprefix("(?i)")), re.IGNORECASE)
    hits = list(dict.fromkeys(window(exact, ln) for ln in lines if exact.search(ln)))
    misses = list(
        dict.fromkeys(
            window(loose, ln) for ln in lines if loose.search(ln) and not exact.search(ln)
        )
    )
    out = [(pattern, ln[:NEGATIVE_CHARS]) for ln in rng.sample(lines, PER_KIND)]
    for group in (hits, misses):
        out.extend((pattern, ln) for ln in rng.sample(group, min(PER_KIND, len(group))))
    for ln in hits[:TRAP_SOURCES]:
        out.extend((pattern, trap) for trap in traps(pattern, ln))
    return out


def main() -> None:
    lines = corpus_lines(sys.argv[1])
    rng = random.Random(SEED)
    compiled = [s.pattern for s in (*SKILLS, *CODE_SKILLS)] + list(eligibility._TOPICS.patterns)
    patterns = sorted({p for c in compiled if (p := inline_pattern(c))})
    cases = dict.fromkeys(case for p in patterns for case in cases_for(p, lines, rng))
    with open(sys.argv[2], "w") as out:
        for pattern, line in cases:
            record = {"fn": "searches", "args": [pattern, line]}
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"{len(lines)} lines, {len(cases)} cases, {len(patterns)} patterns", file=sys.stderr)


if __name__ == "__main__":
    main()
