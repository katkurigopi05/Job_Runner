"""Enforce structured location consistency for every database writer.

Nullable legacy rows remain valid; search preferences are enforced by the crawler.
Constraint expressions are frozen here so future extractor changes cannot alter history.
"""

from alembic import op

revision = "a3c5e7f9b1d2"
down_revision = "f2b4d6e8a0c1"
branch_labels = None
depends_on = None

CONSTRAINTS = (
    (
        "ck_postings_country_code",
        ("country_code IS NULL OR country_code ~ '^[A-Z]{2}$'"),
    ),
    (
        "ck_postings_state_country",
        (
            "state_code IS NULL OR (country_code IS NOT NULL AND country_"
            "code = 'US' AND state_code ~ '^(AK|AL|AR|AZ|CA|CO|CT|DC|DE|F"
            "L|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|NC|ND|N"
            "E|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|W"
            "V|WY)$')"
        ),
    ),
    (
        "ck_postings_locations_array",
        ("locations_json IS NULL OR jsonb_typeof(locations_json) = 'array'"),
    ),
    (
        "ck_postings_location_version",
        ("location_version IS NULL OR (location_version > 0 AND locations_json IS NOT NULL)"),
    ),
    (
        "ck_postings_location_offices",
        (
            "locations_json IS NULL OR NOT jsonb_path_exists(locations_js"
            'on, \'$[*] ? (@.type() != "object" || !exists(@.raw) || @.raw'
            '.type() != "string" || @.raw == "" || (exists(@.country_code'
            ') && @.country_code != null && (@.country_code.type() != "st'
            'ring" || !(@.country_code like_regex "^[A-Z]{2}$"))) || (exi'
            "sts(@.state_code) && @.state_code != null && (!exists(@.coun"
            'try_code) || @.country_code != "US" || @.state_code.type() !'
            '= "string" || !(@.state_code like_regex "^(AK|AL|AR|AZ|CA|CO'
            "|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS"
            "|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA"
            '|VT|WA|WI|WV|WY)$"))) || (exists(@.is_remote) && @.is_remote'
            ' != null && @.is_remote.type() != "boolean") || (exists(@.so'
            "urce_remote) && @.source_remote != null && @.source_remote.t"
            'ype() != "boolean"))\')'
        ),
    ),
    (
        "ck_postings_location_summary",
        (
            "(country_code IS NULL OR locations_json IS NULL OR NOT jsonb"
            "_path_exists(locations_json, '$[*] ? (!exists(@.country_code"
            ") || @.country_code == null || @.country_code != $value)', j"
            "sonb_build_object('value', country_code))) AND (state_code I"
            "S NULL OR locations_json IS NULL OR NOT jsonb_path_exists(lo"
            "cations_json, '$[*] ? (!exists(@.state_code) || @.state_code"
            " == null || @.state_code != $value)', jsonb_build_object('va"
            "lue', state_code))) AND (is_remote IS NULL OR locations_json"
            " IS NULL OR NOT jsonb_path_exists(locations_json, '$[*] ? (!"
            "exists(@.is_remote) || @.is_remote == null || @.is_remote !="
            " $value)', jsonb_build_object('value', is_remote)))"
        ),
    ),
)


def upgrade() -> None:
    for name, sql in CONSTRAINTS:
        op.create_check_constraint(name, "postings", sql)


def downgrade() -> None:
    for name, _ in reversed(CONSTRAINTS):
        op.drop_constraint(name, "postings", type_="check")
