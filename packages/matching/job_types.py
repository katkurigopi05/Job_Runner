# ruff: noqa: E501 — one rule per line. A pattern wrapped across lines no longer
# reads as the single alternation it is, and the table is read top to bottom.
"""A job type for any posting title, for exports and reports.

`roles.py` answers a narrower question for the matcher — which titles name
the *same* engineering role — and it is deliberately curated and small, so
everything else it sees is "no role". That made it the wrong tool for a
column meant to describe every posting: exported through it, 80% of the
owner's postings read "Other", including "AI Researcher" and every sales,
product and legal role.

This is a display taxonomy instead. Rules are tried in order and the first
match wins, so the specific ones come first: "Engineering Manager" before
"Engineer", "Sales Engineer" before "Software Engineer", "Product Marketing"
before both "Product Manager" and "Marketing". The order is the logic; reading
it top to bottom is how to find why a title landed where it did.

It reads the title only. A description would catch more, but it also names
every team the role works with, and "works closely with Sales" is not a sales
job. A title nothing matches is "Other" — a gap to read, not a guess.
"""

from __future__ import annotations

import re

#: (job type, pattern on the lower-cased title, unless-pattern). First match
#: wins; a rule whose unless-pattern also matches is skipped.
_RULES: tuple[tuple[str, str, str | None], ...] = (
    # Internships only. A new-grad Software Engineer is a Software Engineer:
    # seniority is not a job type, and filing them here hid them from it.
    (
        "Intern",
        r"\bintern(ship)?\b|\bsummer 20\d\d\b|apprentice|working student|werkstudent|\bco-?op\b|\bstagiaire\b|\bstage\b",
        None,
    ),
    (
        "Legal & Compliance",
        r"\bcounsel\b|attorney|lawyer|\blegal\b|paralegal|compliance|regulatory",
        None,
    ),
    (
        "Trust & Safety / Policy",
        r"trust (and|&) safety|enforcement|\babuse\b|integrity|content moderat|\bpolicy\b|public affairs|external affairs|government affairs|global affairs",
        None,
    ),
    (
        "Engineering Manager",
        r"engineering manager|manager,? (of )?(software )?engineering|^(senior |sr\.? |staff |principal |associate )?manager\b.*engineering|(director|head|vp|vice president)[ ,]+(of )?(software )?engineering|engineering (director|lead|leader)",
        None,
    ),
    ("Technical Program Manager", r"technical program manag|\btpm\b", None),
    ("Product Marketing", r"product marketing", None),
    (
        "Product Manager",
        r"product manag|product owner|feature owner|(head|director|vp|vice president)[ ,]+(of )?product\b|\bproduct (lead|leader)\b|\bgroup pm\b",
        None,
    ),
    ("Program / Project Manager", r"program manag|project manag|\bpmo\b|delivery manag", None),
    (
        "Designer",
        r"design(er)?\b(?! verification)|\bux\b|\bui\b(?! engineer)|creative director|illustrator|animator",
        None,
    ),
    ("Forward Deployed Engineer", r"forward[- ]deployed|\bdeployed engineer", None),
    (
        "Solutions / Sales Engineer",
        r"solutions? (architect|engineer|consultant|lead)|deployment lead|sales engineer|pre-?sales|customer engineer|value engineer|applied ai architect|partner (solutions|engineer)|field (application|solutions) engineer|implementation engineer",
        None,
    ),
    ("Architect", r"\barchitect\b|field cto", None),
    (
        "Research Scientist / Engineer",
        r"research (scientist|engineer)|\bresearcher\b|\bresidency\b|\bscientist, research\b|member of technical staff,? research",
        None,
    ),
    (
        "Security Engineer",
        r"security|appsec|\bsecops\b|detection (and response )?engineer|threat|offensive|penetration|red team",
        None,
    ),
    (
        "AI / ML Engineer",
        r"machine learning|\bml\b|\bmle\b|\bai engineer|\bai/ml\b|applied ai|deep learning|\bllm\b|mlops|computer vision|perception engineer|\bnlp\b|inference engineer|ai (infrastructure|platform)",
        None,
    ),
    (
        "Data Engineer",
        r"data engineer|analytics engineer|\betl\b|data (platform|infrastructure) engineer|data warehouse",
        None,
    ),
    (
        "Data Scientist",
        r"data scientist|decision scientist|applied scientist|\bquant(itative)? (researcher|developer|analyst)|\bscientist\b",
        None,
    ),
    (
        "Finance & Accounting",
        r"financ|accountant|accounting|accounts (payable|receivable)|collections|internal control|controller|\bfp&a\b|\btax\b|treasury|payroll|\baudit|billing|revenue (accountant|operations analyst)",
        None,
    ),
    (
        "Data / Business Analyst",
        r"data analyst|business analyst|analytics|business intelligence|\bbi (analyst|developer)\b|\banalyst\b",
        None,
    ),
    (
        "DevOps / SRE / Platform",
        r"site reliability|\bsre\b|devops|platform engineer|infrastructure engineer|cloud engineer|production engineer|reliability engineer",
        None,
    ),
    ("Mobile Engineer", r"\bios\b|android|mobile", None),
    ("Frontend Engineer", r"front[- ]?end|\bui engineer|web (developer|engineer)", None),
    ("Full Stack Engineer", r"full[- ]?stack", None),
    ("Backend Engineer", r"back[- ]?end|server[- ]side|\bapi engineer", None),
    (
        "QA / Test Engineer",
        r"\bqa\b|quality assurance|\bsdet\b|test (automation )?engineer|\bqe\b|quality engineer",
        None,
    ),
    ("Embedded / Firmware Engineer", r"embedded|firmware|kernel|\bbsp\b|device driver", None),
    (
        "Robotics Engineer",
        r"robotic|autonomy|motion planning|controls engineer|\bslam\b",
        r"\bmanager\b|commodity|sourcing",
    ),
    (
        "Hardware Engineer",
        r"hardware|electrical|mechanical|\basic\b|\bfpga\b|silicon|\brf\b|analog|\bpcb\b|manufacturing engineer|thermal|structural|propulsion|avionics|aerospace|optical|photonics|design verification|validation engineer|process engineer|materials engineer|battery|power (electronics|engineer)",
        r"software|planner|buyer|sourcing|procurement|commodity",
    ),
    ("Systems Engineer", r"systems engineer", None),
    (
        "Customer Success / Support",
        r"customer success|partner success|support delivery|customer support|customer care|customer experience|support (engineer|specialist|agent|representative|analyst|manager)|technical support|implementation (manager|consultant|specialist|lead)|onboarding|\bhelp ?desk\b|service desk",
        None,
    ),
    (
        "Developer Relations",
        r"developer (relations|advocate|experience)|devrel|developer evangelist",
        None,
    ),
    (
        "Software Engineer",
        r"software|developer|tech lead|programmer|\bswe\b|\bsde\b|member of technical staff|\bmts\b|product engineer|\bcoder\b|engineer, (applications|product)|tools engineer|compiler",
        None,
    ),
    (
        "Sales Development Rep",
        r"sales development|account development|business development rep|\bsdr\b|\bbdr\b|lead generation",
        None,
    ),
    (
        "Account Executive",
        r"account executive|\bae\b|sales executive|field sales|agente di commercio|commercial terrain|sales representative|inside sales|sales rep\b",
        None,
    ),
    (
        "Account Manager",
        r"account manag|account associate|account director|client (partner|manager|director)|key account|relationship manag",
        None,
    ),
    (
        "Partnerships / Business Development",
        r"partnership|business affairs|business development|alliance|channel|partner manag|ecosystem",
        None,
    ),
    ("Sales", r"\bsales\b|revenue|commercial|go-to-market|\bgtm\b", None),
    (
        "Marketing",
        r"marketing|\bbrand\b|content|communications|growth|demand gen|\bevents?\b|social media|community|\bseo\b|\bpr\b|copywriter|editor|writer",
        None,
    ),
    (
        "Recruiting / People",
        r"recruit|sourcer|global mobility|employee relations|offboarding|talent|\bpeople\b|\bhr\b|human resources|\bbenefits\b|compensation|learning (and|&) development|workplace experience",
        None,
    ),
    (
        "Strategy & Operations",
        r"strateg|business operations|\bbizops\b|chief of staff|engagement manag|consultant|operations|\bops\b|supply chain|logistics|procurement|sourcing|planner|planning",
        None,
    ),
    (
        "Administrative / Facilities / IT",
        r"executive assistant|administrative|\badmin\b|office|facilit|workplace|receptionist|\bit (support|specialist|engineer|administrator|manager)\b|systems administrator|coordinator",
        None,
    ),
    (
        "Technician / Operator",
        r"technician|operator|mechanic|\bdriver\b|assembler|machinist|\bwelder\b|warehouse|fulfil+ment|material handler|\bsorter\b|production (supervisor|lead|associate|worker)|shift lead",
        None,
    ),
    (
        "Leadership",
        r"\bchief\b|\bvp\b|vice president|head of|\bdirector\b|general manager|\bpresident\b|\bfounder\b",
        None,
    ),
    ("Engineer (other)", r"engineer", None),
    ("Manager (other)", r"manag", None),
)

_COMPILED = tuple(
    (label, re.compile(pattern), re.compile(unless) if unless else None)
    for label, pattern, unless in _RULES
)

#: Every type this can return, in rule order, plus "Other".
JOB_TYPES: tuple[str, ...] = tuple(label for label, _, _ in _RULES) + ("Other",)


def job_type(title: str | None) -> str:
    """The job type a posting title names, or "Other" when no rule matches."""
    text = " ".join((title or "").lower().split())
    for label, pattern, unless in _COMPILED:
        if pattern.search(text) and not (unless and unless.search(text)):
            return label
    return "Other"


__all__ = ["JOB_TYPES", "job_type"]
