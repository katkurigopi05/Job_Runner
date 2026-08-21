import asyncio
import csv
import re
import urllib.parse
from pathlib import Path
import httpx
import yaml

CSV_PATH = Path("bay_area_tech_companies.csv")
SEEDS_PATH = Path("seeds/companies.yaml")

def get_slugs_from_url(url: str) -> list[str]:
    """Extract possible ATS slugs from a company website URL."""
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        
        # e.g., 15five.com -> '15five', '15fivecom'
        parts = netloc.split(".")
        if len(parts) >= 2:
            base = parts[-2]
            return [base, base + parts[-1], "".join(parts)]
    except Exception:
        pass
    return []

async def check_ats(client: httpx.AsyncClient, name: str, slug: str) -> tuple[str, str, str] | None:
    """Try to find the ATS for a given slug. Returns (ats_type, slug, name) if found."""
    # We use small timeout to fail fast
    endpoints = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{slug}",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "lever": f"https://api.lever.co/v0/postings/{slug}"
    }

    for ats, url in endpoints.items():
        try:
            resp = await client.get(url, timeout=3.0)
            if resp.status_code == 200:
                # Basic check to ensure it's not a generic 200 page
                try:
                    data = resp.json()
                    # Greenhouse has 'name', Ashby has 'jobBoard', Lever is a list
                    if ats == "greenhouse" and "name" in data:
                        return (ats, slug, name)
                    elif ats == "ashby" and "jobBoard" in data:
                        return (ats, slug, name)
                    elif ats == "lever" and isinstance(data, list):
                        return (ats, slug, name)
                except Exception:
                    pass
        except httpx.RequestError:
            continue
    return None

async def discover_for_company(client: httpx.AsyncClient, name: str, url: str) -> tuple[str, str, str] | None:
    slugs = get_slugs_from_url(url)
    # Also add a slug based on the company name
    name_slug = re.sub(r'[^a-z0-9]', '', name.lower())
    if name_slug and name_slug not in slugs:
        slugs.append(name_slug)

    for slug in slugs:
        if not slug: continue
        result = await check_ats(client, name, slug)
        if result:
            return result
    return None

async def main():
    if not CSV_PATH.exists():
        print(f"Error: {CSV_PATH} not found.")
        return

    # Load existing to avoid duplicates
    existing_slugs = set()
    if SEEDS_PATH.exists():
        with open(SEEDS_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            for c in data.get("companies", []):
                existing_slugs.add(c.get("slug"))

    # Read CSV
    companies = []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader) # skip header
        for row in reader:
            if len(row) >= 2:
                companies.append((row[0], row[1]))

    # Take a subset, e.g., the first 50 companies for testing
    subset = companies[:100]
    print(f"Scanning {len(subset)} companies to discover ATS URLs...")

    found = []
    async with httpx.AsyncClient() as client:
        tasks = [discover_for_company(client, name, url) for name, url in subset]
        results = await asyncio.gather(*tasks)
        for r in results:
            if r and r[1] not in existing_slugs:
                found.append(r)
                existing_slugs.add(r[1])

    if not found:
        print("No new ATS boards discovered in this batch.")
        return

    print(f"\nDiscovered {len(found)} new ATS boards!")
    for ats, slug, name in found:
        print(f"  - {name} ({ats}: {slug})")

    # Append to yaml
    with open(SEEDS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {"companies": []}

    for ats, slug, name in found:
        data["companies"].append({
            "name": name,
            "slug": slug,
            "ats": ats
        })

    with open(SEEDS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    
    print("\nSuccessfully appended to seeds/companies.yaml")

if __name__ == "__main__":
    asyncio.run(main())
