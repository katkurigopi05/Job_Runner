"""What `posting_stats` is asked, and what it answers.

Every field is a fixed choice or a name checked against a table, because the
caller is a model (the MCP tool, and later the assistant): a role the table
does not know, a city outside the Bay Area or a title with no role words is
refused with the reason, never read as something close.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from packages.matching.locality import bay_area_city_names
from packages.matching.roles import ROLE_ALIASES, canonical, normalize

DEFAULT_TOP = 10
MAX_TOP = 50
#: Long enough for any role, city or title's role words, short enough to
#: refuse a paragraph before a validator reads it.
MAX_NAME_LENGTH = 80
#: Every role in the table twice over (two spellings each), and no more.
MAX_ROLE_NAMES = 2 * len(ROLE_ALIASES)
MAX_CITIES = 10

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH)
]


class Place(StrEnum):
    #: The owner's standing area, as the feed and the assistant apply it.
    SEARCH_AREA = "search_area"
    EVERYWHERE = "everywhere"
    #: An office in California, the Bay Area included.
    CALIFORNIA = "california"
    #: An office in the Bay Area, or in the `cities` named.
    BAY_AREA = "bay_area"


class WorkMode(StrEnum):
    ANY = "any"
    REMOTE = "remote"
    #: On-site or hybrid: everything `reads_as_remote` does not call remote.
    NOT_REMOTE = "not_remote"


class GroupBy(StrEnum):
    #: The role table: nine engineering and data roles.
    ROLE = "role"
    #: A title's role words, with level and qualifiers removed.
    TITLE = "title"
    COMPANY = "company"


def _role_key(name: str) -> str:
    key = name if name in ROLE_ALIASES else canonical(name)
    if key is None:
        known = ", ".join(sorted(ROLE_ALIASES))
        raise ValueError(f"unknown role {name!r}; the role table knows {known}")
    return key


def _role_keys(names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_role_key(name) for name in names))


def _title_words(title: str | None) -> str | None:
    if title is None:
        return None
    words = normalize(title)
    if not words:
        raise ValueError(f"title {title!r} has no role words, such as 'product manager'")
    return words


class StatsRequest(BaseModel):
    """The postings to count (`roles` … `work_mode`) and the share to measure among them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    roles: tuple[Name, ...] = Field((), max_length=MAX_ROLE_NAMES)
    title: Name | None = None
    place: Place = Place.SEARCH_AREA
    cities: tuple[Name, ...] = Field((), max_length=MAX_CITIES)
    work_mode: WorkMode = WorkMode.ANY
    share_roles: tuple[Name, ...] = Field((), max_length=MAX_ROLE_NAMES)
    share_title: Name | None = None
    share_work_mode: WorkMode = WorkMode.ANY
    group_by: GroupBy | None = None
    top: int = Field(DEFAULT_TOP, ge=1, le=MAX_TOP)

    @field_validator("roles", "share_roles")
    @classmethod
    def _known_roles(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        return _role_keys(names)

    @field_validator("title", "share_title")
    @classmethod
    def _role_words(cls, title: str | None) -> str | None:
        return _title_words(title)

    @field_validator("cities")
    @classmethod
    def _bay_area_cities(cls, cities: tuple[str, ...]) -> tuple[str, ...]:
        names: list[str] = []
        for city in cities:
            found = bay_area_city_names(city)
            if len(found) != 1:
                raise ValueError(f"{city!r} is not one Bay Area city")
            names.append(found[0])
        return tuple(dict.fromkeys(names))

    @model_validator(mode="after")
    def _answerable(self) -> StatsRequest:
        if self.cities and self.place is not Place.BAY_AREA:
            raise ValueError("cities are Bay Area cities, so they need place 'bay_area'")
        if self.roles and self.share_roles and not set(self.roles) & set(self.share_roles):
            # A data engineer posting is never a software engineer posting, so
            # the share would be 0% by construction, and read as a finding.
            raise ValueError(
                "share_roles names none of roles, so its share is 0 by construction; "
                "drop roles to compare the two"
            )
        return self

    @property
    def asks_share(self) -> bool:
        return bool(
            self.share_roles or self.share_title or self.share_work_mode is not WorkMode.ANY
        )


class StatsGroup(BaseModel):
    label: str
    count: int
    percentage: float | None


class PostingStats(BaseModel):
    """Counts of open postings. Every number is a count of rows, or one divided by another."""

    #: The postings counted, in words: "data engineer postings with an office in California".
    scope: str
    total: int
    #: What the share is of, in words, when one was asked for: "remote".
    share_of: str | None = None
    matching: int | None = None
    percentage: float | None = None
    group_by: GroupBy | None = None
    groups: tuple[StatsGroup, ...] = ()
    #: Postings in groups beyond the `top` shown.
    other: int = 0
    #: Postings the grouping cannot label: a title the role table does not
    #: know, a title with no role words, a posting with no company.
    unclassified: int = 0
    #: How the counts were read, for whoever words the answer.
    notes: tuple[str, ...] = ()
