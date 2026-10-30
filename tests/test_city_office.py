"""City office evidence must not widen to a different city in the same region."""

import pytest

from packages.matching.locality import bay_area_city_names, names_city_office


@pytest.mark.parametrize(
    "location,title,wanted,expected",
    [
        ("SF, CA", "Engineer", ("san francisco",), True),
        ("San Francisco, CA", "Engineer", ("san francisco",), True),
        ("South San Francisco, CA", "Engineer", ("san francisco",), False),
        ("South San Francisco, CA", "Engineer", ("south san francisco",), True),
        ("San Jose, CA", "Engineer", ("san francisco",), False),
        ("Toronto, ON; San Francisco, CA", "Engineer", ("san francisco",), True),
        ("San Jose, Costa Rica", "Engineer", ("san jose",), False),
        ("San Francisco, TX", "Engineer", ("san francisco",), False),
        ("Remote, US", "Engineer - SF", ("san francisco",), False),
        ("Remote", "Engineer - SF", ("san francisco",), True),
        ("Austin, TX", "Engineer - SF", ("san francisco",), False),
        ("Remote", "Engineer", ("san francisco",), False),
    ],
)
def test_city_office(location, title, wanted, expected):
    assert names_city_office(location, title=title, cities=wanted) is expected


def test_complete_city_names_and_region_labels_are_distinct():
    assert bay_area_city_names("jobs in SF or San Jose") == ("san francisco", "san jose")
    assert bay_area_city_names("South San Francisco") == ("south san francisco",)
    assert bay_area_city_names("Bay Area or Silicon Valley") == ()
