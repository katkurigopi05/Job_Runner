import asyncio
import csv
import re
import urllib.parse
from pathlib import Path
import httpx

from packages.crawler.extract import EXTRACTORS, ExtractedPosting

CSV_PATH = Path("bay_area_tech_companies.csv")

def get_slugs_from_url(url: str) -> list[str]:
    try:
        parsed = urllib.parse.urlparse(url)
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        parts = netloc.split(".")
        if len(parts) >= 2:
            base = parts[-2]
            return [base, base + parts[-1], "".join(parts)]
    except Exception:
        pass
    return []

def is_sde_role(title: str) -> bool:
    if not title:
        return False
    title_lower = title.lower()
    return bool(re.search(r'\bsde\b', title_lower) or 
                "software development engineer" in title_lower)

async def check_ats_and_fetch(client: httpx.AsyncClient, name: str, slug: str) -> list[ExtractedPosting]:
    found_postings = []
    for ats, extractor in EXTRACTORS.items():
        url = extractor.board_url(slug)
        try:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code == 200:
                try:
                    # Some extractors parse the body and return a list of ExtractedPosting
                    postings = extractor.parse(resp.text, slug)
                    if postings:
                        return postings
                except Exception:
                    pass
        except httpx.RequestError:
            continue
    return []

async def process_company(client: httpx.AsyncClient, name: str, url: str) -> list[tuple[str, ExtractedPosting]]:
    slugs = get_slugs_from_url(url)
    name_slug = re.sub(r'[^a-z0-9]', '', name.lower())
    if name_slug and name_slug not in slugs:
        slugs.append(name_slug)

    sde_postings = []
    for slug in slugs:
        if not slug: continue
        postings = await check_ats_and_fetch(client, name, slug)
        if postings:
            for p in postings:
                if is_sde_role(p.title):
                    sde_postings.append((name, p))
            break # Once we successfully fetched an ATS board for this company, stop trying other slugs
    return sde_postings

async def main():
    if not CSV_PATH.exists():
        print(f"Error: {CSV_PATH} not found.")
        return

    companies = []
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if len(row) >= 2:
                companies.append((row[0], row[1]))

    print(f"Processing {len(companies)} companies from the CSV. This might take a minute...")
    
    # We will use a Semaphore to limit concurrency
    sem = asyncio.Semaphore(50)
    
    async def bound_process(c_name, c_url):
        async with sem:
            return await process_company(client, c_name, c_url)

    all_sde = []
    async with httpx.AsyncClient() as client:
        tasks = [bound_process(name, url) for name, url in companies]
        
        # Gather with return_exceptions=True so one failure doesn't kill the batch
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for res in results:
            if isinstance(res, list):
                all_sde.extend(res)

    if not all_sde:
        print("\nNo open SDE roles found in the entire CSV list today.")
        return

    print(f"\nFound {len(all_sde)} open SDE roles!")
    for company_name, posting in all_sde:
        print(f"Company: {company_name}")
        print(f"Title: {posting.title}")
        print(f"Location: {posting.location}")
        print(f"URL: {posting.url}")
        print("-" * 40)

if __name__ == "__main__":
    asyncio.run(main())
