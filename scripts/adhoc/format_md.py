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

with open("/Users/gopikrishnareddykatkuri/.gemini/antigravity-cli/brain/c0d862b8-cad9-4fc6-beea-ec486a2e8f56/live_sde_roles.md", "w") as out:
    out.write("# Live SDE Roles from CSV\n\n")
    out.write(f"Found {len(companies)} open SDE roles directly from the ATS boards of the companies in the CSV.\n\n")
    out.write("| Company | Title | Location | Link |\n")
    out.write("|---------|-------|----------|------|\n")
    for c in companies:
        out.write(f"| {c['company']} | {c['title']} | {c['location']} | [Apply]({c['url']}) |\n")
