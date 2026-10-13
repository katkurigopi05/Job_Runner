"""Text a model left for someone else to fill in.

"Dear [Hiring Manager]," and "at {company}" are what a model writes when it
does not know. §2.4 says an unanswerable question is never left blank, and a
placeholder is a blank with brackets round it: typed into an employer's form,
it says the letter was not read by whoever sent it.

Neither check that decides what a model may send looked for one. The
fabrication guard caught a few by accident (`[Company Name]`, because "Name"
traces to nothing in a résumé) and missed the plain ones: `[company]` is
three ordinary lowercase letters and has nothing to trace. A letter's greeting
and sign-off are set aside before the guard reads it at all, and that is
where `[Hiring Manager]` and `[Your Name]` are.

What counts:

- text in square brackets, braces or angle brackets that has a letter in it
  (`[company]`, `{{company_name}}`, `<your name>`);
- a run of capital X standing in for a figure (`XX%`, `$XXX`).

What does not: a reference number (`[1]`), a comparison (`< 100 ms`), an
address in angle brackets, round brackets of any kind, and **anything the
owner wrote themselves**. Their résumé says `[GitHub]` beside a project, so a
caller passes the text it already knows and a span found there is theirs.
"""

from __future__ import annotations

import re

_BRACKETED = re.compile(
    r"\[[^\[\]\n]*[A-Za-z][^\[\]\n]*\]"  # [Company Name], [X]
    r"|\{\{?[^{}\n]*[A-Za-z][^{}\n]*\}\}?"  # {company}, {{company_name}}
    r"|<[A-Za-z][A-Za-z _-]*>"  # <company>, <your name>; not <jane@example.com>, not < 100
    r"|(?<![A-Za-z0-9])X{2,}(?![A-Za-z0-9])"  # XX%, $XXX; not XXL
)


def unfilled(text: str, *, known: str = "") -> list[str]:
    """Each blank left in `text`, in order, less what `known` already says."""
    theirs = known.casefold()
    return [
        match.group(0)
        for match in _BRACKETED.finditer(text)
        if not theirs or match.group(0).casefold() not in theirs
    ]


def refusal(text: str, *, known: str = "") -> str | None:
    """Why `text` may not be sent, or None when it leaves no blank."""
    left = unfilled(text, known=known)
    if not left:
        return None
    return f"leaves a blank for someone else to fill in: {left[0]!r}"


__all__ = ["refusal", "unfilled"]
