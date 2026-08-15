"""Pre-flight check on whether an application can possibly be completed.

`incomplete_candidate` is knowable before any browser starts: a profile with no
phone number or no work-authorization answer will fail on every ATS form there
is. So it is rejected at `POST /applications` with `invalid_request` and no row
is created, rather than being enqueued to fail later.

The contrast is `job_closed`, which genuinely needs the browser to discover and
so stays a worker-side failure reason.
"""

from __future__ import annotations

from packages.core.models import Candidate, Profile

#: Profile fields an ATS form will always demand. Extend as adapters land.
REQUIRED_PROFILE_FIELDS: tuple[str, ...] = (
    "phone",
    "location",
    "work_auth",
)

#: Fields whose *answer* may legitimately be False, so presence is what counts
#: rather than truthiness.
REQUIRED_PROFILE_BOOLEAN_FIELDS: tuple[str, ...] = ("needs_sponsorship",)

REQUIRED_CANDIDATE_FIELDS: tuple[str, ...] = ("name", "email")


def missing_requirements(candidate: Candidate, profile: Profile) -> list[str]:
    """Return the dotted names of everything needed to apply that is absent.

    Empty list means the application can proceed.
    """
    missing: list[str] = []

    for field in REQUIRED_CANDIDATE_FIELDS:
        value = getattr(candidate, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(f"candidate.{field}")

    for field in REQUIRED_PROFILE_FIELDS:
        value = getattr(profile, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(f"profile.{field}")

    # A False answer is a real answer; only None means unanswered.
    for field in REQUIRED_PROFILE_BOOLEAN_FIELDS:
        if getattr(profile, field, None) is None:
            missing.append(f"profile.{field}")

    # NOTE: `profile.base_resume_id` belongs on this list, but résumé upload
    # does not exist until Phase 2 — requiring it now would make every
    # application unsubmittable. Add it to REQUIRED_PROFILE_FIELDS when
    # Phase 2 lands.

    return missing
