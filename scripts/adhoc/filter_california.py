import re

with open("sde_results.txt") as f:
    lines = f.readlines()

companies = []
current = {}
for line in lines:
    line = line.strip()
    if line.startswith("Company: "):
        current["company"] = line.split("Company: ", 1)[1]
    elif line.startswith("Title: "):
        current["title"] = line.split("Title: ", 1)[1]
    elif line.startswith("Location: "):
        current["location"] = line.split("Location: ", 1)[1]
    elif line.startswith("URL: "):
        current["url"] = line.split("URL: ", 1)[1]
    elif line.startswith("---"):
        if current:
            companies.append(current)
            current = {}

bay_area_keywords = [
    "san francisco", "sf", "bay area", "palo alto", "san jose", "santa clara",
    "sunnyvale", "mountain view", "california", "menlo park", "cupertino",
    "redwood city", "san mateo", "oakland", "berkeley", "foster city",
    "usca", "remote - california", "remote, california"
]

bay_area_jobs = []
for c in companies:
    loc = c.get("location", "").lower()
    if "india" in loc or "ind" in loc or "bangalore" in loc or "mohali" in loc or "bengaluru" in loc or "canada" in loc:
        continue
    
    is_bay_area = False
    for kw in bay_area_keywords:
        if kw in loc:
            is_bay_area = True
            break
            
    if not is_bay_area and re.search(r'\bca\b', loc):
        is_bay_area = True

    if is_bay_area:
        bay_area_jobs.append(c)

with open("/Users/gopikrishnareddykatkuri/.gemini/antigravity-cli/brain/c0d862b8-cad9-4fc6-beea-ec486a2e8f56/california_sde_roles.md", "w") as out:
    out.write("# California Bay Area SDE Roles\n\n")
    out.write(f"Found {len(bay_area_jobs)} open SDE roles in California / Bay Area.\n\n")
    out.write("| Company | Title | Location | Link |\n")
    out.write("|---------|-------|----------|------|\n")
    for c in bay_area_jobs:
        out.write(f"| {c['company']} | {c['title']} | {c['location']} | [Apply]({c['url']}) |\n")
