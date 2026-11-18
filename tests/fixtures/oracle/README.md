# Public fixture provenance — 2026-11-18

Career page: https://ehap.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX

The site's referenced JavaScript: https://static.oracle.com/cdn/fa/oj-hcm-ce/2607.26.262300322/js/main-minimal.js

List request: https://ehap.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=CX,facetsList=NONE,limit=25,offset=0

Read using `User-Agent: jobrunner` through `build_fetcher(shared=True,
delay_seconds=5, record_metrics=False)`. PoliteFetcher checked robots.txt
and reserved shared host slots, honoring any longer server delay. No login,
captcha, application, secured API, paid API or alternate identity was used.
Each list and the representative public detail returned HTTP 200.
No company/posting rows were written or promoted by these probes.

The fixtures retain source values; they omit unrelated keys, analytics IDs,
links and most list rows. Totals remain the real advertised totals, so these
trimmed lists are deliberately incomplete. Tests that need a complete small
board explicitly rewrite the total or offset and label those scenarios synthetic.

Camden advertised 54 jobs. List fixtures retain two jobs each from offsets 0
and 25; the second live page contained different IDs. The detail fixture retains
the public fields for job 260670, from `recruitingCEJobRequisitionDetails` with
`expand=all&onlyData=true&finder=ById;Id="260670",siteNumber=CX`. It contains the
original public description, without contact fields or unrelated metadata.
The JavaScript sets the base `/hcmRestApi/resources/latest`, uses `findReqs`
with `siteNumber`, `limit`, `offset`, and reads job details with `ById`.

Original downloaded list SHA-256: `82c2ab96bc1772d2b17ff5f0b84c008def8b516c50e3d1b02783bb5f05265ae4`.
