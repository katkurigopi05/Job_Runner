"""Where a posting is, read from the free text a board wrote it in.

The owner's search is United States only, California first. That is two
questions and this answers both: `locality_of` classifies a location string,
and `rank` orders the classes. Nothing here scores — §1 keeps the filter and
the score apart, and a Texan posting the owner would love should still rank
above a Californian one they would not.

## Why this is not a keyword list

The obvious version is a list of Bay Area cities and a `"ca" in location`
check, and it is wrong in two directions that matter.

**"CA" is two countries.** `San Jose, CA` is California and `Toronto, ON, CA`
is Canada, both in the same two letters. So country is decided *before* `CA`
is read, and only then can a bare `CA` mean California.

**Substrings match the wrong words.** `"ind" in location` catches India, and
also **Ind**iana, Bloom**ind**gton and **Ind**ependence, MO — three American
places filtered out as Indian ones. Every check here is word-boundaried, and
the two-letter state codes additionally require the comma that precedes them
in an American address, because `OR`, `IN`, `OK`, `HI`, `ME`, `DE` and `LA`
are all ordinary English words that appear in job postings.

`Vancouver, WA` is the case that decides the order of the rules: Vancouver is
a Canadian city and Washington is an American state, so an explicit state code
has to outrank a city name. Country *names* are checked first, state codes
second, and ambiguous city names last.

## What it cannot do

This reads text, not geography. A posting that says only "Remote" is
`UNKNOWN`, not American — the caller decides whether to keep those, and
`SearchFilters.allow_unknown_location` is where that decision lives. Guessing
would be worse than saying so.
"""

from __future__ import annotations

import re
from enum import StrEnum


class Locality(StrEnum):
    """Where a posting sits relative to the owner's search."""

    BAY_AREA = "bay_area"
    CALIFORNIA = "california"
    UNITED_STATES = "united_states"
    ELSEWHERE = "elsewhere"
    #: No location text at all. No evidence either way, so not a reason to
    #: hide a job.
    UNKNOWN = "unknown"
    #: Location text that no rule here could place. Distinct from `UNKNOWN`
    #: on purpose: across the 66 postings of the first real board sweep,
    #: *every* unplaceable string was foreign — "Mapbox Minsk", "Mapbox UK".
    #: Silence is not evidence; an unrecognized place name is.
    UNPLACED = "unplaced"


#: Ordering for the feed: lower sorts first. `UNKNOWN` sits below anything
#: placed and above anything foreign, which is the honest position for it —
#: worth showing, not worth showing first.
_RANK = {
    Locality.BAY_AREA: 0,
    Locality.CALIFORNIA: 1,
    Locality.UNITED_STATES: 2,
    Locality.UNKNOWN: 3,
    Locality.UNPLACED: 4,
    Locality.ELSEWHERE: 5,
}

#: Everything the owner's search treats as domestic.
US_LOCALITIES = frozenset({Locality.BAY_AREA, Locality.CALIFORNIA, Locality.UNITED_STATES})

_BAY_AREA_CITIES = (
    "san francisco",
    "bay area",
    "silicon valley",
    "palo alto",
    "menlo park",
    "mountain view",
    "sunnyvale",
    "santa clara",
    "san jose",
    "cupertino",
    "redwood city",
    "san mateo",
    "foster city",
    "burlingame",
    "millbrae",
    "belmont",
    "san carlos",
    "los altos",
    "campbell",
    "milpitas",
    "fremont",
    "hayward",
    "oakland",
    "berkeley",
    "emeryville",
    "alameda",
    "san leandro",
    "walnut creek",
    "pleasanton",
    "livermore",
    "san ramon",
    "concord",
    "richmond",
    "novato",
    "san rafael",
    "sausalito",
    "south san francisco",
    "brisbane",
    "daly city",
    "santa cruz",
)

_CALIFORNIA_CITIES = (
    "los angeles",
    "san diego",
    "sacramento",
    "irvine",
    "san bernardino",
    "long beach",
    "anaheim",
    "santa monica",
    "pasadena",
    "burbank",
    "culver city",
    "el segundo",
    "costa mesa",
    "carlsbad",
    "fresno",
    "bakersfield",
    "san luis obispo",
    "santa barbara",
    "ventura",
    "riverside",
    "torrance",
    "glendale",
    "sunnyvale",
    "playa vista",
    "marina del rey",
    "west hollywood",
)

#: Country names and non-US subdivision codes. Unambiguous enough to decide
#: the question before anything else is read.
_NON_US_MARKERS = (
    r"canada",
    r"united kingdom",
    r"england",
    r"scotland",
    r"wales",
    r"ireland",
    r"india",
    r"germany",
    r"france",
    r"spain",
    r"portugal",
    r"netherlands",
    r"belgium",
    r"switzerland",
    r"austria",
    r"poland",
    r"romania",
    r"czechia",
    r"czech republic",
    r"hungary",
    r"sweden",
    r"norway",
    r"denmark",
    r"finland",
    r"italy",
    r"greece",
    r"israel",
    r"turkey",
    r"japan",
    r"china",
    r"singapore",
    r"australia",
    r"new zealand",
    r"brazil",
    r"mexico",
    r"argentina",
    r"chile",
    r"colombia",
    r"south africa",
    r"nigeria",
    r"kenya",
    r"egypt",
    r"philippines",
    r"indonesia",
    r"vietnam",
    r"thailand",
    r"malaysia",
    r"pakistan",
    r"bangladesh",
    r"sri lanka",
    r"south korea",
    r"korea",
    r"taiwan",
    r"hong kong",
    r"emirates",
    r"uae",
    r"saudi",
    r"qatar",
    r"u\.k\.",
    r"uk",
    r"emea",
    r"apac",
    r"latam",
    r"cemea",
)

_NON_US_RE = re.compile(r"\b(?:" + "|".join(_NON_US_MARKERS) + r")\b", re.I)

#: Canadian and other non-US subdivision codes, in the same comma position an
#: American state code occupies. `, ON` is Ontario; `, BC` is British Columbia.
_NON_US_SUBDIVISION_RE = re.compile(r",\s*(?:ON|QC|BC|AB|MB|SK|NS|NB|NL|PE|NT|YT|NU)\b")

#: Cities that place a posting abroad, but only once no American state code
#: has claimed it — `Vancouver, WA` is in Washington.
_NON_US_CITIES_RE = re.compile(
    r"\b(?:toronto|vancouver|montreal|montréal|ottawa|calgary|edmonton|winnipeg|halifax|"
    r"waterloo|mississauga|london|dublin|edinburgh|manchester|bristol|cambridge|"
    r"bengaluru|bangalore|hyderabad|mumbai|pune|chennai|delhi|gurgaon|gurugram|noida|mohali|"
    r"berlin|munich|hamburg|paris|lyon|madrid|barcelona|lisbon|amsterdam|rotterdam|"
    r"brussels|zurich|geneva|vienna|warsaw|krakow|bucharest|prague|budapest|"
    r"stockholm|oslo|copenhagen|helsinki|milan|rome|athens|"
    r"tel aviv|istanbul|dubai|abu dhabi|riyadh|doha|"
    r"tokyo|osaka|beijing|shanghai|shenzhen|seoul|taipei|"
    r"sydney|melbourne|brisbane|perth|auckland|wellington|"
    r"são paulo|sao paulo|rio de janeiro|mexico city|guadalajara|bogota|bogotá|"
    r"buenos aires|santiago|lagos|nairobi|cairo|cape town|johannesburg|"
    r"manila|jakarta|hanoi|bangkok|kuala lumpur|karachi|lahore|dhaka|colombo|"
    r"minsk|kyiv|kiev|moscow|belgrade|zagreb|sofia|tallinn|riga|vilnius)\b",
    re.I,
)

#: State code to full name, so a search for one finds the other.
#:
#: Employers write the same place both ways — "San Francisco, CA" and "San
#: Francisco, California" sit side by side in this corpus — so a location
#: filter that knows only the form the owner typed silently drops half the
#: matches. The module already carries both vocabularies for *classification*;
#: this is the same knowledge in the form a *search* needs.
#:
#: DC is included because postings write it as a state.
STATE_BY_CODE: dict[str, str] = {
    "AL": "alabama",
    "AK": "alaska",
    "AZ": "arizona",
    "AR": "arkansas",
    "CA": "california",
    "CO": "colorado",
    "CT": "connecticut",
    "DE": "delaware",
    "DC": "district of columbia",
    "FL": "florida",
    "GA": "georgia",
    "HI": "hawaii",
    "ID": "idaho",
    "IL": "illinois",
    "IN": "indiana",
    "IA": "iowa",
    "KS": "kansas",
    "KY": "kentucky",
    "LA": "louisiana",
    "ME": "maine",
    "MD": "maryland",
    "MA": "massachusetts",
    "MI": "michigan",
    "MN": "minnesota",
    "MS": "mississippi",
    "MO": "missouri",
    "MT": "montana",
    "NE": "nebraska",
    "NV": "nevada",
    "NH": "new hampshire",
    "NJ": "new jersey",
    "NM": "new mexico",
    "NY": "new york",
    "NC": "north carolina",
    "ND": "north dakota",
    "OH": "ohio",
    "OK": "oklahoma",
    "OR": "oregon",
    "PA": "pennsylvania",
    "RI": "rhode island",
    "SC": "south carolina",
    "SD": "south dakota",
    "TN": "tennessee",
    "TX": "texas",
    "UT": "utah",
    "VT": "vermont",
    "VA": "virginia",
    "WA": "washington",
    "WV": "west virginia",
    "WI": "wisconsin",
    "WY": "wyoming",
}

#: The reverse, built rather than typed twice.
CODE_BY_STATE: dict[str, str] = {name: code for code, name in STATE_BY_CODE.items()}


def location_aliases(term: str) -> tuple[str, ...]:
    """Every spelling of `term` a posting might use, including `term` itself.

    "CA" yields ("ca", "california"); "California" yields ("california", "ca").
    Anything that is not a US state is returned unchanged — this widens a
    search, it does not reinterpret it.
    """
    cleaned = term.strip().lower()
    if not cleaned:
        return ()
    if (name := STATE_BY_CODE.get(cleaned.upper())) is not None:
        return (cleaned, name)
    if (code := CODE_BY_STATE.get(cleaned)) is not None:
        return (cleaned, code.lower())
    return (cleaned,)


_US_STATE_CODES = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
)

#: A state code only counts where an American address puts it — after the
#: comma that follows the city. Bare `OR` is the word "or".
_US_STATE_CODE_RE = re.compile(r",\s*(" + "|".join(_US_STATE_CODES) + r")\b")

#: Multi-word names first so the alternation prefers "new york" over "new".
_US_STATE_NAMES = (
    "new hampshire",
    "new jersey",
    "new mexico",
    "new york",
    "north carolina",
    "north dakota",
    "rhode island",
    "south carolina",
    "south dakota",
    "west virginia",
    "district of columbia",
    "alabama",
    "alaska",
    "arizona",
    "arkansas",
    "colorado",
    "connecticut",
    "delaware",
    "florida",
    "georgia",
    "hawaii",
    "idaho",
    "illinois",
    "indiana",
    "iowa",
    "kansas",
    "kentucky",
    "louisiana",
    "maine",
    "maryland",
    "massachusetts",
    "michigan",
    "minnesota",
    "mississippi",
    "missouri",
    "montana",
    "nebraska",
    "nevada",
    "ohio",
    "oklahoma",
    "oregon",
    "pennsylvania",
    "tennessee",
    "texas",
    "utah",
    "vermont",
    "virginia",
    "washington",
    "wisconsin",
    "wyoming",
)

_US_STATE_NAME_RE = re.compile(r"\b(?:" + "|".join(_US_STATE_NAMES) + r")\b", re.I)

_US_COUNTRY_RE = re.compile(r"\b(?:united states|usa|u\.s\.a\.|u\.s\.|us)\b", re.I)

#: Boards very often write only the city — "Austin", "Seattle" — with no state
#: and no country. Without these the commonest form of American location is
#: `UNPLACED` and a US-only search throws away most of its own results.
#:
#: Checked *after* the state codes, which is what settles the shared names:
#: `Cambridge, MA` and `Manchester, NH` are American by their state code
#: before the city is read, and a bare "Cambridge" stays foreign.
_US_CITIES = (
    "new york city",
    "nyc",
    "brooklyn",
    "manhattan",
    "queens",
    "seattle",
    "bellevue",
    "redmond",
    "tacoma",
    "spokane",
    "austin",
    "dallas",
    "houston",
    "san antonio",
    "fort worth",
    "plano",
    "denver",
    "boulder",
    "colorado springs",
    "aurora",
    "boston",
    "somerville",
    "waltham",
    "burlington",
    "chicago",
    "evanston",
    "naperville",
    "atlanta",
    "alpharetta",
    "savannah",
    "portland",
    "eugene",
    "beaverton",
    "hillsboro",
    "phoenix",
    "scottsdale",
    "tempe",
    "mesa",
    "tucson",
    "miami",
    "orlando",
    "tampa",
    "jacksonville",
    "fort lauderdale",
    "nashville",
    "memphis",
    "knoxville",
    "chattanooga",
    "philadelphia",
    "pittsburgh",
    "harrisburg",
    "salt lake city",
    "provo",
    "park city",
    "lehi",
    "minneapolis",
    "st paul",
    "saint paul",
    "rochester",
    "detroit",
    "ann arbor",
    "grand rapids",
    "raleigh",
    "durham",
    "chapel hill",
    "charlotte",
    "winston-salem",
    "columbus",
    "cleveland",
    "cincinnati",
    "dayton",
    "kansas city",
    "st louis",
    "saint louis",
    "omaha",
    "des moines",
    "las vegas",
    "reno",
    "henderson",
    "baltimore",
    "annapolis",
    "bethesda",
    "arlington",
    "alexandria",
    "reston",
    "washington dc",
    "washington, d.c.",
    "d.c.",
    "new orleans",
    "baton rouge",
    "birmingham",
    "huntsville",
    "indianapolis",
    "louisville",
    "milwaukee",
    "madison",
    "albuquerque",
    "boise",
    "anchorage",
    "honolulu",
    "richmond",
    "virginia beach",
    "norfolk",
    "charleston",
    "greenville",
    "hartford",
    "stamford",
    "new haven",
    "providence",
    "princeton",
    "hoboken",
    "jersey city",
    "newark",
)

_US_CITY_RE = re.compile(r"\b(?:" + "|".join(re.escape(c) for c in _US_CITIES) + r")\b", re.I)

_CALIFORNIA_RE = re.compile(r"\bcalifornia\b", re.I)
_CALIFORNIA_CODE_RE = re.compile(r"\bCA\b")

_BAY_AREA_RE = re.compile(r"\b(?:" + "|".join(_BAY_AREA_CITIES) + r")\b", re.I)
_CALIFORNIA_CITY_RE = re.compile(r"\b(?:" + "|".join(_CALIFORNIA_CITIES) + r")\b", re.I)


def _is_california(text: str, original: str) -> bool:
    return bool(_CALIFORNIA_RE.search(text) or _CALIFORNIA_CODE_RE.search(original))


def locality_of(location: str | None) -> Locality:
    """Classify a posting's location string.

    Rule order is the whole design; see the module docstring for why it is
    this order and not a more obvious one.
    """
    original = (location or "").strip()
    if not original:
        return Locality.UNKNOWN

    text = original.lower()

    # 1. An explicit foreign country or subdivision settles it, before `CA`
    #    can be misread as California.
    # A foreign *subdivision* code is decisive and has no escape. `Toronto,
    # ON, CA` ends in the same two letters as a Californian address, and the
    # province is the half that means something.
    if _NON_US_SUBDIVISION_RE.search(original):
        return Locality.ELSEWHERE

    # A foreign *country name* can share a posting with an American one —
    # "Remote - US or Canada" is a job the owner can take — so that yields to
    # an explicit US signal. A bare "CA" is not one of those signals here:
    # inside a Canadian address it is the country, not the state.
    if _NON_US_RE.search(text) and not (
        _US_STATE_CODE_RE.search(original)
        or _CALIFORNIA_RE.search(text)
        or _US_COUNTRY_RE.search(text)
    ):
        return Locality.ELSEWHERE

    # 2. An explicit non-Californian state code settles it before any city
    #    name is read. `Newark, NJ` and `Richmond, VA` both share a name with
    #    a Bay Area city, and the state is the half that means something —
    #    the same rule that keeps `Vancouver, WA` out of Canada.
    codes = set(_US_STATE_CODE_RE.findall(original))
    if codes and "CA" not in codes:
        return Locality.UNITED_STATES

    # 3. California, at the finest grain the string supports.
    if _BAY_AREA_RE.search(text):
        return Locality.BAY_AREA
    if _is_california(text, original) or _CALIFORNIA_CITY_RE.search(text):
        return Locality.CALIFORNIA

    # 4. Anywhere else in the United States.
    if (
        _US_STATE_CODE_RE.search(original)
        or _US_STATE_NAME_RE.search(text)
        or _US_COUNTRY_RE.search(text)
        or _US_CITY_RE.search(text)
    ):
        return Locality.UNITED_STATES

    # 5. A foreign city, now that no American state has claimed the string.
    if _NON_US_CITIES_RE.search(text):
        return Locality.ELSEWHERE

    return Locality.UNPLACED


def rank(locality: Locality) -> int:
    """Feed order: Bay Area, then California, then the rest of the US."""
    return _RANK[locality]


def is_domestic(locality: Locality) -> bool:
    """Whether this counts as inside the owner's search area."""
    return locality in US_LOCALITIES
