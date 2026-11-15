"""Structured source locations. NULL means the source cannot settle a field."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from packages.matching.locality import (
    STATE_BY_CODE,
    US_LOCALITIES,
    Locality,
    _canada_by_its_parts,
    _fold,
    _us_state_codes,
    locality_of,
    locations_in,
    reads_as_remote,
    wholly_abroad,
)

LOCATION_VERSION = 1
type OfficeFacts = tuple[tuple[str, str | None, str | None, bool | None], ...]


def office_facts(
    offices: list[dict[str, Any]] | None,
) -> OfficeFacts:
    return tuple(
        (
            office["raw"],
            office.get("country_code"),
            office.get("state_code"),
            office.get("is_remote"),
        )
        for office in offices or []
        if isinstance(office.get("raw"), str)
    )


def wholly_foreign(reading: dict[str, Any]) -> bool:
    offices = reading["locations_json"]
    return bool(offices) and all(
        office["country_code"] != "US"
        and (office["country_code"] is not None or wholly_abroad(office["raw"]))
        for office in offices
    )


# ISO codes and source names, rather than a country guessed from company HQ.
_COUNTRIES = {
    "US": "USA|United States|United States of America|U.S.",
    "GB": "GBR|UK|United Kingdom|England|Scotland|Wales",
    "CA": "CAN|Canada",
    "IE": "IRL|Ireland",
    "DE": "DEU|Germany|Deutschland",
    "FR": "FRA|France",
    "IT": "ITA|Italy",
    "ES": "ESP|Spain",
    "PT": "PRT|Portugal",
    "NL": "NLD|Netherlands",
    "BE": "BEL|Belgium",
    "CH": "CHE|Switzerland",
    "AT": "AUT|Austria",
    "PL": "POL|Poland",
    "RO": "ROU|Romania",
    "CZ": "CZE|Czechia|Czech Republic",
    "HU": "HUN|Hungary",
    "SE": "SWE|Sweden",
    "NO": "NOR|Norway",
    "DK": "DNK|Denmark",
    "FI": "FIN|Finland",
    "IN": "IND|India",
    "CN": "CHN|China",
    "JP": "JPN|Japan",
    "KR": "KOR|South Korea|Korea",
    "TW": "TWN|Taiwan",
    "HK": "HKG|Hong Kong",
    "SG": "SGP|Singapore",
    "PH": "PHL|Philippines",
    "MY": "MYS|Malaysia",
    "ID": "IDN|Indonesia",
    "TH": "THA|Thailand",
    "VN": "VNM|Vietnam",
    "PK": "PAK|Pakistan",
    "BD": "BGD|Bangladesh",
    "LK": "LKA|Sri Lanka",
    "AU": "AUS|Australia",
    "NZ": "NZL|New Zealand",
    "MX": "MEX|Mexico",
    "BR": "BRA|Brazil",
    "AR": "ARG|Argentina",
    "CO": "COL|Colombia",
    "CL": "CHL|Chile",
    "PE": "PER|Peru",
    "CR": "CRI|Costa Rica",
    "SV": "SLV|El Salvador",
    "VE": "VEN|Venezuela",
    "IL": "ISR|Israel",
    "AE": "ARE|United Arab Emirates|UAE",
    "SA": "SAU|Saudi Arabia",
    "QA": "QAT|Qatar",
    "ZA": "ZAF|South Africa",
    "NG": "NGA|Nigeria",
    "KE": "KEN|Kenya",
    "EG": "EGY|Egypt",
    "LY": "LBY|Libya",
    "GY": "GUY|Guyana",
    "UA": "UKR|Ukraine",
    "RU": "RUS|Russia",
    "TR": "TUR|Turkey",
    "GR": "GRC|Greece",
    "BG": "BGR|Bulgaria",
    "LU": "LUX|Luxembourg",
}
_ALIASES = {
    _fold(alias).casefold(): code
    for code, aliases in _COUNTRIES.items()
    for alias in [code, *aliases.split("|")]
}
_COUNTRY_NAMES = [
    (re.compile(r"\b" + re.escape(_fold(alias)) + r"\b", re.I), code)
    for code, aliases in _COUNTRIES.items()
    for alias in aliases.split("|")
    if len(alias) > 3 and alias != "U.S."
]
_SOURCE_CODE = re.compile(
    r"^\(?([A-Z]{2,3})\)?(?=[\s,|\-–—]|$)|\(([A-Z]{2,3})\)"
    r"|[,\-–—]\s*([A-Z]{2,3})\s*$"
)
_ONSITE = re.compile(r"\b(?:hybrid|on[- ]?site|in[- ]?office|in[- ]?person)\b", re.I)


def country_code(value: Any) -> str | None:
    """An explicit source country name/code, including Workday descriptors."""
    if isinstance(value, dict):
        value = value.get("alpha2Code") or value.get("descriptor") or value.get("name")
    return _ALIASES.get(_fold(value).strip().casefold()) if isinstance(value, str) else None


@lru_cache(maxsize=8192)
def _country(raw: str) -> str | None:
    where = locality_of(raw)
    if where in US_LOCALITIES:
        return "US"
    for pattern, code in _COUNTRY_NAMES:
        if pattern.search(_fold(raw)):
            return code
    for match in _SOURCE_CODE.finditer(raw):
        token = next(group for group in match.groups() if group)
        # Colliding postal codes need positive foreign evidence first.
        if token in STATE_BY_CODE and where is not Locality.ELSEWHERE:
            continue
        if explicit_code := country_code(token):
            return explicit_code
    return None


@lru_cache(maxsize=8192)
def _state(raw: str, country: str | None) -> str | None:
    if country != "US":
        return None
    if re.search(r"\bWashington[ ,]+D\.?C\.?\b|\bD\.C\.", raw, re.I):
        return "DC"
    codes = _us_state_codes(raw)
    if not codes:
        codes = {
            code
            for code, name in STATE_BY_CODE.items()
            if re.search(r"\b" + re.escape(name) + r"\b", raw, re.I)
        }
        if locality_of(raw) in {Locality.BAY_AREA, Locality.CALIFORNIA} and codes - {"CA"}:
            return None
    if len(codes) == 1:
        return next(iter(codes))
    if not codes and locality_of(raw) in {Locality.BAY_AREA, Locality.CALIFORNIA}:
        return "CA"
    return None


def normalize(
    location: str | None,
    *,
    title: str | None = None,
    description: str | None = None,
    source_locations: list[dict[str, Any]] | None = None,
    source_remote: bool | None = None,
) -> dict[str, Any]:
    offices = source_locations or [{"raw": part} for part in locations_in(location)]
    if not source_locations and _canada_by_its_parts(locations_in(location)):
        offices = [{"raw": location or "", "country_code": "CA"}]
    normalized = []
    for office in offices:
        raw = office.get("raw")
        if not isinstance(raw, str) or not raw.strip():
            continue
        country = country_code(office.get("country_code")) or _country(raw)
        explicit_remote = (
            source_remote if source_remote is not None else office.get("source_remote")
        )
        remote = explicit_remote
        if remote is None:
            if _ONSITE.search(" ".join([raw, title or ""])):
                remote = False
            elif reads_as_remote(title=title, location=raw, description=description):
                remote = True
        normalized.append(
            {
                "raw": raw,
                "country_code": country,
                "state_code": _state(raw, country),
                "source_remote": explicit_remote,
                "is_remote": remote,
            }
        )
    countries = {office["country_code"] for office in normalized}
    states = {office["state_code"] for office in normalized}
    modes = {office["is_remote"] for office in normalized}
    remote = next(iter(modes)) if len(modes) == 1 else source_remote
    if not normalized and remote is None:
        if _ONSITE.search(" ".join([location or "", title or ""])):
            remote = False
        elif reads_as_remote(title=title, location=location, description=description):
            remote = True
    return {
        "country_code": next(iter(countries)) if len(countries) == 1 else None,
        "state_code": next(iter(states)) if len(states) == 1 else None,
        "is_remote": remote,
        "locations_json": normalized,
        "location_version": LOCATION_VERSION,
    }
