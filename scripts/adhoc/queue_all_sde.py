import json
import httpx
import re

def main():
    candidate_id = "dde2d209-217f-4b8f-8557-e5bd4f8bf2bf"
    profile_id = "f61303d8-1517-4d1e-9ae1-9ac46257543a"
    
    urls = []
    with open("sde_results.txt") as f:
        for line in f:
            if line.startswith("URL: "):
                urls.append(line.split("URL: ")[1].strip())

    print(f"Queueing {len(urls)} applications...")
    
    queued = 0
    failed = 0
    
    with httpx.Client() as client:
        for url in urls:
            payload = {
                "candidate_id": candidate_id,
                "profile_id": profile_id,
                "url": url
            }
            try:
                resp = client.post("http://127.0.0.1:8000/applications", json=payload)
                if resp.status_code in (200, 201):
                    queued += 1
                else:
                    print(f"Failed to queue {url}: {resp.status_code} - {resp.text}")
                    failed += 1
            except Exception as e:
                print(f"Error on {url}: {e}")
                failed += 1

    print(f"\nFinished! Successfully queued {queued} applications. Failed: {failed}")

if __name__ == "__main__":
    main()
