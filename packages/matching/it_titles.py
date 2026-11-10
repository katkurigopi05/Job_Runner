"""Which postings are IT roles, from the title alone.

The owner's focus, 2026-11-09: IT roles only. Used where a title is all there
is — a Workday listing carries no description until its page is read, and the
description backfill reads only the pages worth reading.

The vocabulary follows the BLS 2018 SOC computer occupations (15-1211 to
15-1299: systems analysts, information security, network support and
architecture, user support, database administration and architecture, systems
administration, programmers, software developers, QA and testers, web
developers), plus 11-3021 computer and information systems managers and
15-2051 data scientists, with the titles employers post today (cloud, DevOps,
SRE, ML/AI, SAP and Salesforce). https://www.bls.gov/soc/2018/major_groups.htm

`roles.py` is a different thing: it claims two titles are the *same job*.
This only says a title is in the IT sector, so it may be broad.

Some groups are IT-adjacent rather than IT (hardware design, product and
program management, UX design, tech consulting and pre-sales). They have
labels so a caller can see and count them, and are not IT unless named.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable

#: Not IT whatever else the title says: a guard, a typist, a health plan's network, sales.
_NOT_IT = re.compile(
    r"\b(security (officer|guard|agent)|loss prevention|data entry|provider network|"
    r"network (development|management) (manager|director|consultant)|"
    r"front[- ]end (coach|associate|cashier|supervisor|service)|"
    r"(?<!technical )account (executive|manager)|"
    r"sales (representative|executive|specialist|manager|director|lead|operations)|"
    r"(director|head|vp|vice president),? (of )?sales|"
    r"business development (manager|representative|director|executive|associate))\b",
    re.I,
)
#: "IT" is a word only in capitals; "it" is a pronoun.
_IT_WORD = re.compile(r"(?<![A-Za-z])IT(?![A-Za-z])")

#: IT-adjacent groups, tried before the IT ones: their noun decides the job.
ADJACENT: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "product_program",
        re.compile(
            r"\b(product (manager|owner|management|lead)|technical program manage(r|ment)|"
            r"program manager,? (it|software|technology|technical|data|cloud|engineering)|"
            r"scrum master|agile coach|release manager)\b",
            re.I,
        ),
    ),
    (
        "ux_design",
        re.compile(
            r"\b(ux|ui|user experience|user interface|interaction|product|web|digital) "
            r"(designer|design lead|researcher)\b|\bux\b|\bui/ux\b",
            re.I,
        ),
    ),
    (
        "tech_sales_consulting",
        re.compile(
            r"\b(sales engineer|solutions? (consultant|engineer|specialist)|pre-?sales|"
            r"technology (consultant|consulting)|technical account manager|"
            r"customer engineer|field application engineer)\b",
            re.I,
        ),
    ),
)

#: IT groups, in the order tried.
IT: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "it_leadership",
        re.compile(
            r"\b(chief (technology|information|information security|data|digital) officer|"
            r"\bcto\b|\bcio\b|\bciso\b|(software|data|platform|infrastructure|cloud|devops|"
            r"machine learning|ml|ai) engineering (manager|director|lead)|"
            r"(director|head|vp|vice president)(,| of)? (software |data |platform |cloud |"
            r"information |it |)(engineering|technology|information technology|"
            r"information systems)|information systems (manager|director)|"
            r"information technology (manager|director))\b",
            re.I,
        ),
    ),
    (
        "security",
        re.compile(
            r"\b(cyber\w*|information security|infosec|(application|cloud|network|product|"
            r"data|it|endpoint|offensive|threat|identity) security|security (engineer|analyst|"
            r"architect|operations|consultant|specialist|researcher|administrator)|"
            r"penetration test\w*|pen ?test\w*|red team|blue team|soc analyst|"
            r"identity and access|\biam\b|vulnerability|incident response|it risk|"
            r"threat (intelligence|hunt\w*|detection)|devsecops|grc analyst|"
            r"technology (risk|compliance))\b",
            re.I,
        ),
    ),
    (
        "data_ai",
        re.compile(
            r"\b(data (engineer\w*|scien\w*|analyst|analytics|architect\w*|platform|"
            r"warehous\w*|integration|modell?er|governance|infrastructure|ops|pipeline\w*|"
            r"visualization|mining|quality engineer|product manager)|big data|analytics|"
            r"business intelligence|\bbi\b|\betl\b|database|\bdba\b|\bsql\b|"
            r"machine learning|\bml\b|\bmlops\b|deep learning|artificial intelligence|"
            r"\bai\b|gen ?ai|generative|\bllm\w*|\bnlp\b|natural language|computer vision|"
            r"applied (scientist|research\w*)|research engineer|operations research|"
            r"robotics research\w*|quantum|technical research|master data|\bmdm\b|"
            r"research scientist,? (ai|ml|machine)|quantitative developer|"
            r"snowflake|databricks|tableau|power bi|apache spark|pyspark|hadoop)\b",
            re.I,
        ),
    ),
    (
        "enterprise_apps",
        re.compile(
            r"\b(sap|abap|salesforce|servicenow|workday (developer|integration\w*|analyst|"
            r"consultant|engineer|architect|administrator|hcm|financials)|"
            r"oracle (developer|dba|ebs|cloud|fusion|apps|consultant|engineer)|"
            r"dynamics 365|\bd365\b|netsuite|\berp\b|\bcrm\b (developer|administrator|analyst)|"
            r"sharepoint|guidewire|pega|mulesoft|business systems analyst|"
            r"business analyst|business systems? analyst|systems analyst)\b",
            re.I,
        ),
    ),
    (
        "cloud_infra",
        re.compile(
            r"\b(devops|site reliability|\bsre\b|platform engineer\w*|infrastructure "
            r"(engineer|architect|analyst|administrator|specialist)|cloud (engineer|architect|"
            r"developer|administrator|operations|specialist|consultant|infrastructure|"
            r"platform)|\baws\b|azure|\bgcp\b|google cloud|kubernetes|terraform|"
            r"network (engineer|administrator|admin|analyst|architect|technician|"
            r"specialist|operations|support)|systems? (engineer|administrator|admin|"
            r"architect|programmer|integrator)|sysadmin|linux|unix|windows (engineer|"
            r"administrator)|storage (engineer|administrator)|data cent(er|re) "
            r"(technician|operator|engineer|operations)|vmware|citrix|"
            r"active directory|middleware|mainframe|release engineer|build engineer)\b",
            re.I,
        ),
    ),
    (
        "it_support",
        re.compile(
            r"\b(help ?desk|service desk|desktop support|end[- ]user (support|computing)|"
            r"technical support (engineer|specialist|analyst|technician)|tech support|"
            r"it support|computer (technician|operator|support)|field services? technician,? it|"
            r"information technology|information systems|\bmis\b|deskside)\b",
            re.I,
        ),
    ),
    (
        "software",
        re.compile(
            r"\b(software|\bsw\b|developer|programmer|coder|\bsde\b|\bswe\b|"
            r"member of technical staff|\bmts\b|front[- ]?end|back[- ]?end|full[- ]?stack|"
            r"web (developer|engineer|architect|applications?)|mobile (developer|engineer|"
            r"application\w*|app\w*)|\bios\b|android|firmware|embedded (software|systems|"
            r"engineer)|applications? (engineer|developer|architect|analyst|support)|"
            r"(solutions?|software|enterprise|technical|integration|api) architect|"
            r"integration (engineer|developer|specialist)|api (developer|engineer|platform)|"
            r"sdet|test automation|"
            r"automation (engineer|developer|tester)|(qa|quality assurance) (engineer|"
            r"analyst|automation|tester)|software (quality|test\w*)|computer scien\w*|"
            r"computer (engineer|programmer|systems)|technical lead|tech lead|"
            r"java|python|\.net|c\+\+|c#|golang|javascript|typescript|react|node\.?js|"
            r"game (developer|programmer|engineer)|graphics engineer|compiler|"
            r"robotics software|autonomy)\b",
            re.I,
        ),
    ),
)

HARDWARE = (
    "hardware",
    re.compile(
        r"\b(hardware|asic|fpga|silicon|\bsoc\b|\bic\b|rtl|vlsi|analog|mixed[- ]signal|"
        r"physical design|design verification|verification engineer|dft|chip|"
        r"semiconductor|board design|pcb|signal integrity)\b",
        re.I,
    ),
)

IT_GROUPS: tuple[str, ...] = tuple(name for name, _ in IT)
ADJACENT_GROUPS: tuple[str, ...] = (*(name for name, _ in ADJACENT), HARDWARE[0])


def it_group(title: str | None) -> str | None:
    """The IT or IT-adjacent group a title belongs to, or None when neither."""
    if not title or not title.strip():
        return None
    if _NOT_IT.search(title):
        return None
    for name, pattern in ADJACENT:
        if pattern.search(title):
            return name
    for name, pattern in IT:
        if pattern.search(title):
            return name
    if _IT_WORD.search(title):
        return "it_support"
    name, pattern = HARDWARE
    return name if pattern.search(title) else None


def check_adjacent(include: Iterable[str]) -> frozenset[str]:
    """The IT-adjacent groups named, refusing a name that is not one."""
    groups = frozenset(include)
    unknown = groups - set(ADJACENT_GROUPS)
    if unknown:
        raise ValueError(f"unknown IT-adjacent groups: {', '.join(sorted(unknown))}")
    return groups


def is_it_role(title: str | None, *, include: Collection[str] = ()) -> bool:
    """Whether a title is an IT role; `include` adds IT-adjacent groups by name."""
    groups = check_adjacent(include)
    group = it_group(title)
    return group is not None and (group in IT_GROUPS or group in groups)
