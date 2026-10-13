"""What a robots.txt says about a URL — RFC 9309.

Kept apart from `robots.py`, which fetches the file and decides what an
unreadable one means. This reads one.

`urllib.robotparser` did this until 2026-10-12. It follows the 1994 note the
RFC replaced, and on five ordinary files it answered "allowed" for a path the
site had disallowed:

- a byte-order mark hid the first `User-agent`, and with it every rule in
  its group;
- a blank line after `User-agent` ended the record, so the rules under it
  belonged to nobody;
- the first matching rule won, so `Allow: /` written ahead of
  `Disallow: /careers/private` allowed everything;
- `*` and `$` were read as those two characters.

None of them raises. The page is fetched, the cycle looks the same, and §2.6
has been broken on a site that said no. Found by comparison rather than by a
failure: `tools/robots_check.py` in MadsLorentzen/ai-job-search (MIT) had met
the first two and records why Python's parser fails open on them.

Four decisions, each taken on the cautious side:

- **The longest match decides, and a tie goes to Disallow.** The RFC says an
  equal Allow SHOULD win. This is a crawler asking whether it is welcome, and
  of two equal answers it takes the one that says no.
- **Octets, not characters** (§2.2.2). The file is never decoded. A rule and a
  URL are each reduced to the bytes they name, so a path written in Latin-1
  matches the URL that serves it. Decoded with `errors="replace"`, as the file
  used to be, that rule became U+FFFD and matched nothing the site serves.
- **A group ends where the next begins, not at a blank line**, and a line that
  is not a rule (`Sitemap`) does not end one either.
- **A group that names us is the only one that applies**, even when it holds
  no rule: `User-agent: jobrunner` over an empty `Disallow:` is a site letting
  us in, whatever it tells everyone else.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote_to_bytes, urlsplit

#: The group for any crawler no other group names.
ANY_AGENT = "*"

_BOM = b"\xef\xbb\xbf"
#: RFC 9309 §2.2.1: a product token is letters, underscores and hyphens. What
#: follows it in a User-Agent string (a version, a comment) is not the name.
_PRODUCT_TOKEN = re.compile(r"[a-z_-]+")


@dataclass(frozen=True)
class _Rule:
    allow: bool
    #: How specific the rule is: the octets its path names.
    length: int
    pattern: re.Pattern[bytes]


@dataclass(frozen=True)
class Rules:
    """One robots.txt: each group's rules, by the crawler it names."""

    rules: Mapping[str, tuple[_Rule, ...]] = field(default_factory=dict)
    delays: Mapping[str, float] = field(default_factory=dict)

    def _group_for(self, user_agent: str) -> str:
        token = _token(user_agent)
        return token if token and token in self.rules else ANY_AGENT

    def allows(self, user_agent: str, url: str) -> bool:
        """Whether `url` may be fetched by `user_agent`. No matching rule allows."""
        target = _target(url)
        longest, allowed = -1, True
        for rule in self.rules.get(self._group_for(user_agent), ()):
            if rule.pattern.match(target) is None:
                continue
            if rule.length > longest or (rule.length == longest and not rule.allow):
                longest, allowed = rule.length, rule.allow
        return allowed

    def crawl_delay(self, user_agent: str) -> float | None:
        """The seconds the applying group asks for between requests, if it says."""
        return self.delays.get(self._group_for(user_agent))


def _token(agent: str) -> str:
    """A crawler's name as groups are matched on it: lower case, name only."""
    name = agent.strip().lower()
    if name == ANY_AGENT:
        return ANY_AGENT
    found = _PRODUCT_TOKEN.match(name)
    return found.group(0) if found else ""


def _target(url: str) -> bytes:
    """The octets of what a rule is matched against: the path and its query."""
    try:
        parts = urlsplit(url)
    except ValueError:
        # A malformed bracketed host. The fetch fails at the socket; this only
        # has to answer without raising.
        return b"/"
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return unquote_to_bytes(target)


def _rule(allow: bool, value: bytes) -> _Rule:
    octets = unquote_to_bytes(value)
    # `$` anchors only where it ends the rule; anywhere else it is a dollar.
    anchored = octets.endswith(b"$")
    body = octets[:-1] if anchored else octets
    regex = b".*".join(re.escape(part) for part in body.split(b"*"))
    return _Rule(
        allow=allow,
        length=len(octets),
        pattern=re.compile(regex + (rb"\Z" if anchored else b""), re.DOTALL),
    )


def _delay(value: bytes) -> float | None:
    """A Crawl-delay as seconds, or None for anything that is not a wait.

    `inf` and `nan` parse as floats. The first is a host never fetched again
    by a rule nobody wrote, and the second compares false to everything.
    """
    try:
        seconds = float(value.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def parse(body: bytes) -> Rules:
    """Read a robots.txt body. Never raises: a line it cannot read is skipped."""
    rules: dict[str, list[_Rule]] = {}
    delays: dict[str, float] = {}
    agents: list[str] = []
    # True while reading the run of User-agent lines that opens a group. The
    # first rule ends the run, and the next User-agent line starts a new group.
    naming = True
    for raw in body.removeprefix(_BOM).splitlines():
        name, colon, value = raw.split(b"#", 1)[0].partition(b":")
        if not colon:
            continue
        key, value = name.strip().lower(), value.strip()
        if key == b"user-agent":
            if not naming:
                agents, naming = [], True
            agent = _token(value.decode("ascii", errors="replace"))
            if agent not in agents:  # named twice, its rules are still once each
                agents.append(agent)
            # Recorded even with no rule to its name: see the module docstring.
            rules.setdefault(agent, [])
        elif key in (b"allow", b"disallow"):
            naming = False
            if not value:  # `Disallow:` with nothing after it disallows nothing
                continue
            rule = _rule(key == b"allow", value)
            for agent in agents:
                rules[agent].append(rule)
        elif key == b"crawl-delay":
            naming = False
            delay = _delay(value)
            if delay is not None:
                # The longer of two, whichever was written first: a site asking
                # for more space gets it, and the longer honours both.
                for agent in agents:
                    delays[agent] = max(delay, delays.get(agent, delay))
    return Rules(
        rules={agent: tuple(group) for agent, group in rules.items()},
        delays=delays,
    )


__all__ = ["ANY_AGENT", "Rules", "parse"]
