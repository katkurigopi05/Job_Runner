# Notes on the Rust crates

Loads when work touches `rust/`. One crate so far, `pattern-search-rs`, ported
with rust-anything on 2026-11-26.

## pattern-search-rs: the skill matcher

`skill_vocab.find_skills` searched every line of every posting with each of
~81 compiled patterns, about 3,800 `re` searches a posting, and that was 81% of
`requirements.extract` (13 ms a posting; ~11 minutes for a 50,000-posting
`make skill-links`). The crate compiles a vocabulary once and reads a line in
one `RegexSet` pass, confirming the few it finds with `fancy-regex` where a
pattern has a lookaround. Measured on 3,002 postings: `extract` 12.6 ms to
2.0 ms, 6.3x, with identical output for `SKILLS` and `CODE_SKILLS`.

Its second use, 2026-11-27: `eligibility.read_posting`, which the feed's
sponsorship and citizens-only filters run over every match on every request,
with no cache. It looks for its topic words ("sponsor", "citizen", the phrases
of residence) over the whole posting and then per sentence, and runs its
patterns only where they can match. Live, 8,579 matches: those filtered feeds
went from 5.0 s to 0.72 s a request, the same postings kept; over all 20,573
open postings the verdicts are identical to every-sentence reading
(`tests/test_eligibility_topics.py` keeps that reading as its oracle).

Both go through `packages/matching/pattern_set.py`, which loads the crate and
falls back to `re`. A third reader should too, not load the crate itself.

It is optional. `make rust` builds it into `.venv`; without it, or for a
vocabulary it refuses, `find_skills` asks `re` and gets the same answer.
CI builds it and sets `REQUIRE_RUST_MATCHER=1`, so its tests fail there
rather than skip. The Docker image does not build it.

What a later change could undo:

- **Exact, not near.** Rust's `(?i)` and `\s` are not Python's: `(?i)i` misses
  `İ` and `ı`, which Python's `re.IGNORECASE` matches, as `k` matches the
  Kelvin sign and `s` the long s; Rust's `\s` lacks `\x1c`-`\x1f`. The port
  never uses either. It writes every literal, class and `\s` out as the
  characters Python matches (`port.rs`, `case_partners`, `PYTHON_SPACE`), and
  `tests/test_pattern_search.py` re-measures both from the running Python, so
  a Python upgrade that moves them fails a test instead of a filter.
- **The syntax is closed.** `packages/matching/pattern_search.py` is the
  reference: `re.search` over the syntax the vocabularies use, and a
  `ValueError` naming the position for anything else, raised at the same place
  by both. A new skill in other syntax (`\b`, `\w`, `{2}`, a negated class)
  sends its whole vocabulary back to `re` with a warning, and the test that
  every vocabulary pattern is taken fails first. Widen the syntax in both,
  re-record, and gate; do not loosen the check. A quantifier follows only
  something with a character in it: fancy-regex will not compile a repeated
  group that can match only nothing (`(?:)+(?!a)`), which `re` runs. Found by a
  review's fuzz (3.4M comparisons, 0 disagreements after the fix).
- **`find_skills` keeps `re` as the answer of last resort:** a lone surrogate
  cannot cross into Rust, and `fancy-regex` has a backtracking limit `re` does
  not. Both fall back rather than raise.

To change the crate: edit `src/port.rs`, then `rust-anything build`, `diff`
and `gate` in `rust/pattern-search-rs` (all must pass), and `make rust` to
install. After changing `pattern_search.py`, re-record first; `inputs/` has
the generator and how to run it. Record with the project's Python first on
`PATH`: rust-anything runs `python3`, which on this Mac is 3.14, not the
venv's 3.12.

**macOS 27.2.1 (ld-27106) refuses a stripped release dylib**, "mis-aligned
LINKEDIT string pool". Cargo strips debug info from release builds by
default, so the crate sets `strip = false`. A debug build and an unstripped
release build both load.
