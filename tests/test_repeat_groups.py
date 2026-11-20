import uuid
from datetime import UTC, datetime, timedelta

from packages.matching.repeats import ListingIdentity, group_listings


def test_group_order_uses_first_rank_but_rep_is_newest():
    company = uuid.uuid4()
    first, middle, newest = (uuid.uuid4() for _ in range(3))
    now = datetime.now(UTC)
    rows = [
        ListingIdentity(first, company, "Engineer", "digest", now - timedelta(days=1), "SF"),
        ListingIdentity(middle, company, "Other", "digest", now, "LA"),
        ListingIdentity(newest, company, " ENGINEER ", "digest", now, "SJ"),
    ]
    groups = group_listings(rows)
    assert [g.representative for g in groups] == [newest, middle]
    assert groups[0].members == (first, newest)
    assert groups[0].locations == ("SF", "SJ")


def test_unknown_company_or_description_cannot_merge():
    now = datetime.now(UTC)
    company = uuid.uuid4()
    for owner, digest in [(None, "hash"), (company, None), (company, "")]:
        rows = [
            ListingIdentity(uuid.uuid4(), owner, "Engineer", digest, now, "SF") for _ in range(2)
        ]
        assert len(group_listings(rows)) == 2
