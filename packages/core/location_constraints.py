"""Database invariants for structured location facts, independent of search preferences."""

_STATES = (
    "AK|AL|AR|AZ|CA|CO|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|"
    "MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY"
)
_INVALID_OFFICE = (
    '$[*] ? (@.type() != "object" || !exists(@.raw) || @.raw.type() != "string" || @.raw == ""'
    " || (exists(@.country_code) && @.country_code != null &&"
    ' (@.country_code.type() != "string" || !(@.country_code like_regex "^[A-Z]{2}$")))'
    " || (exists(@.state_code) && @.state_code != null &&"
    ' (!exists(@.country_code) || @.country_code != "US" || @.state_code.type() != "string"'
    f' || !(@.state_code like_regex "^({_STATES})$")))'
    ' || (exists(@.is_remote) && @.is_remote != null && @.is_remote.type() != "boolean")'
    " || (exists(@.source_remote) && @.source_remote != null"
    ' && @.source_remote.type() != "boolean"))'
)

LOCATION_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ck_postings_country_code", "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'"),
    (
        "ck_postings_state_country",
        "state_code IS NULL OR (country_code IS NOT NULL AND country_code = 'US' "
        f"AND state_code ~ '^({_STATES})$')",
    ),
    (
        "ck_postings_locations_array",
        "locations_json IS NULL OR jsonb_typeof(locations_json) = 'array'",
    ),
    (
        "ck_postings_location_version",
        "location_version IS NULL OR (location_version > 0 AND locations_json IS NOT NULL)",
    ),
    (
        "ck_postings_location_offices",
        f"locations_json IS NULL OR NOT jsonb_path_exists(locations_json, '{_INVALID_OFFICE}')",
    ),
    (
        "ck_postings_location_summary",
        " AND ".join(
            f"({column} IS NULL OR locations_json IS NULL OR NOT jsonb_path_exists("
            f"locations_json, '$[*] ? (!exists(@.{column}) || @.{column} == null "
            f"|| @.{column} != $value)', jsonb_build_object('value', {column})))"
            for column in ("country_code", "state_code", "is_remote")
        ),
    ),
)
