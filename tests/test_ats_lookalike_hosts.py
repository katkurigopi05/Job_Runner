"""A form is filled only on the ATS it claims to be.

The apply pipeline opens `application.url`, types the owner's profile into
whatever form is there and uploads their résumé. Which sites it will do that
on is decided by each adapter's `matches`, twice: once to choose the adapter,
and again after the page has loaded (`navigate._check_still_ours`). A URL that
only *looks* like an ATS and passes both is the owner's name, phone, work
authorization and résumé typed into somebody else's page, and an application
URL is not always the owner's own typing: discovery resolves links it found
on aggregators, and an assistant can pass one to `apply_to_url`.

On 2026-10-12 one did pass. Greenhouse's embedded-form pattern was searched
for anywhere in the URL, so any site whose address *contained* a Greenhouse
embed address was a Greenhouse form:

    https://evil.example/go?u=https://boards.greenhouse.io/embed/job_app?for=acme

The other three adapters and Greenhouse's main pattern were anchored and
refused every shape below. Found by asking the question
`tests/test_apply_host_check.py` in MadsLorentzen/ai-job-search (MIT) asks of
its own apply step: look-alikes "must fail closed".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.ats.base import UnsupportedSiteError
from packages.ats.greenhouse import GreenhouseAdapter
from packages.ats.navigate import PostingGone, _check_still_ours, over_https
from packages.ats.registry import adapter_for, detect_ats

JOB = "0a1b2c3d-1111-2222-3333-444455556666"

REAL = {
    "https://boards.greenhouse.io/acme/jobs/1234567": "greenhouse",
    "https://job-boards.greenhouse.io/acme/jobs/1234567": "greenhouse",
    "https://boards.greenhouse.io/embed/job_app?for=acme&token=1234567": "greenhouse",
    "https://job-boards.greenhouse.io/embed/job_app?token=1234567&for=acme": "greenhouse",
    "https://boards.eu.greenhouse.io/embed/job_app?for=acme&token=1234567": "greenhouse",
    f"https://jobs.lever.co/acme/{JOB}": "lever",
    f"https://jobs.ashbyhq.com/acme/{JOB}": "ashby",
    "https://apply.workable.com/acme/j/ABCDEF1234/": "workable",
}

LOOK_ALIKES = [
    # The real address carried inside somebody else's.
    "https://evil.example/go?u=https://boards.greenhouse.io/embed/job_app?for=acme&token=1",
    "https://evil.example/greenhouse.io/embed/job_app?for=acme",
    "https://evil.example/?next=https://boards.greenhouse.io/acme/jobs/1234567",
    "https://evil.example/boards.greenhouse.io/acme/jobs/1234567",
    f"https://evil.example/x?u=https://jobs.lever.co/acme/{JOB}",
    f"https://evil.example/x?u=https://jobs.ashbyhq.com/acme/{JOB}",
    "https://evil.example/x?u=https://apply.workable.com/acme/j/ABCDEF1234/",
    # The real host as a label of somebody else's.
    "https://boards.greenhouse.io.evil.example/acme/jobs/1234567",
    "https://boards.greenhouse.io.evil.example/embed/job_app?for=acme",
    f"https://jobs.lever.co.evil.example/acme/{JOB}",
    f"https://jobs.ashbyhq.com.evil.example/acme/{JOB}",
    "https://apply.workable.com.evil.example/acme/j/ABCDEF1234/",
    # The real host where a user name goes.
    "https://boards.greenhouse.io@evil.example/acme/jobs/1234567",
    "https://boards.greenhouse.io@evil.example/embed/job_app?for=acme",
    f"https://jobs.lever.co@evil.example/acme/{JOB}",
    f"https://jobs.ashbyhq.com@evil.example/acme/{JOB}",
    "https://apply.workable.com@evil.example/acme/j/ABCDEF1234/",
    # A name that only resembles it.
    "https://notgreenhouse.io/embed/job_app?for=acme&token=1",
    "https://boards-greenhouse.io/acme/jobs/1234567",
    "https://greenhouse.io.example/embed/job_app?for=acme",
    f"https://notjobs.lever.co.example/acme/{JOB}",
    # Not a web address at all.
    "javascript://boards.greenhouse.io/acme/jobs/1234567",
    "file:///boards.greenhouse.io/embed/job_app?for=acme",
]


@pytest.mark.parametrize("url", sorted(REAL))
def test_a_real_form_is_recognised(url: str) -> None:
    assert detect_ats(url) == REAL[url]


@pytest.mark.parametrize("url", LOOK_ALIKES)
def test_a_look_alike_is_nobodys_form(url: str) -> None:
    assert detect_ats(url) is None
    with pytest.raises(UnsupportedSiteError):
        adapter_for(url)


@pytest.mark.parametrize("url", LOOK_ALIKES)
def test_landing_on_a_look_alike_is_not_still_on_the_ats(url: str) -> None:
    """The second check, made after the page loads. A real form that sends
    the browser on to a look-alike must not be filled either."""
    with pytest.raises((UnsupportedSiteError, PostingGone)):
        _check_still_ours(GreenhouseAdapter, url, external_id=None)


def test_an_embedded_form_still_names_its_company() -> None:
    slug = GreenhouseAdapter.company_slug

    assert slug("https://boards.greenhouse.io/embed/job_app?for=acme&token=1234567") == "acme"
    assert slug("https://job-boards.greenhouse.io/embed/job_app?token=1234567&for=acme") == "acme"
    # `for` has to be the parameter's whole name.
    assert slug("https://boards.greenhouse.io/embed/job_app?notfor=acme") is None


# --------------------------------------------------------------------------
# And only over https
# --------------------------------------------------------------------------


def test_a_form_address_is_opened_over_https() -> None:
    assert over_https("http://boards.greenhouse.io/acme/jobs/1") == (
        "https://boards.greenhouse.io/acme/jobs/1"
    )
    assert over_https("HTTP://jobs.lever.co/acme/x") == "https://jobs.lever.co/acme/x"
    assert over_https("https://jobs.ashbyhq.com/acme/x") == "https://jobs.ashbyhq.com/acme/x"


async def test_the_worker_asks_for_the_form_over_https(monkeypatch) -> None:
    """Every adapter recognises an `http://` address, because a link found on
    a careers page may be written that way. The page the owner's details are
    typed into is asked for over https whatever the link said: a plain
    request can be answered by anyone on the network between here and there.
    """
    from contextlib import asynccontextmanager

    from apps.worker import apply_job

    class Stop(Exception):
        pass

    asked: list[str] = []

    class Page:
        async def goto(self, url: str, **_: object) -> None:
            asked.append(url)
            raise Stop

    @asynccontextmanager
    async def page_for(_ats: str, **_kwargs: object):
        yield Page()

    monkeypatch.setattr(apply_job, "browser_page", page_for)
    application = SimpleNamespace(url="http://boards.greenhouse.io/acme/jobs/1234567", ats=None)

    with pytest.raises(Stop):
        await apply_job._run_pipeline(None, application, None, None)  # type: ignore[arg-type]

    assert asked == ["https://boards.greenhouse.io/acme/jobs/1234567"]
