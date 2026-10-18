import asyncio
import json
from pathlib import Path
from apps.mcp import server
from apps.mcp.client import ApiUnavailable


class FakeClient:
    def __init__(self, unavailable=False):
        self.calls = []
        self.unavailable = unavailable

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.unavailable:
            raise ApiUnavailable("Synthetic unavailable API; no connection attempted")
        if method == "GET":
            return {"id": "fixture-app", "status": "needs_review", "review": {"unanswered": []}, "url": "https://employer.example/job"}
        return {"queued": True, "worker_alive": False, "status": "running"}


async def main():
    tools = await server.server.list_tools()
    Path("/private/tmp/jobrunner-audit-tools-20261015.json").write_text(json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in tools], indent=2, default=str)+"\n")
    client = FakeClient()
    server.set_client(client)
    result = await server.server.call_tool("approve_application", {"application_id": "fixture-app"})
    print(json.dumps({"case": "approval_without_owner_form", "is_error": result.is_error, "posted_approval": any(m=="POST" and k.get("json",{}).get("approve") is True for m,p,k in client.calls)}))
    client = FakeClient()
    server.set_client(client)
    result = await server.server.call_tool("start_crawl", {})
    print(json.dumps({"case": "crawl_without_owner_form", "is_error": result.is_error, "posted_crawl": any(m=="POST" for m,p,k in client.calls)}))
    server.set_client(FakeClient(unavailable=True))
    result = await server.server.call_tool("crawl_status", {})
    print(json.dumps({"case": "api_failure_flag", "is_error": result.is_error, "content": [c.text for c in result.content]}))


asyncio.run(main())
