"""Saved searches: stored apart from the profile, and only as feed filters."""

from __future__ import annotations

from httpx import AsyncClient


async def test_a_saved_search_round_trips(client: AsyncClient) -> None:
    saved = await client.put(
        "/search-preferences/default",
        json={"filters": {"min_salary": "150000", "lacking_skills": "java", "remote": ""}},
    )

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["scope"] == "global"
    assert body["filters"] == {"min_salary": "150000", "lacking_skills": "java"}, (
        "an empty value is a cleared filter, not a stored empty string"
    )

    fetched = (await client.get("/search-preferences/default")).json()
    assert fetched["filters"] == body["filters"]


async def test_saving_again_replaces_rather_than_duplicates(client: AsyncClient) -> None:
    await client.put("/search-preferences/default", json={"filters": {"remote": "true"}})
    await client.put("/search-preferences/default", json={"filters": {"remote": "false"}})

    listed = (await client.get("/search-preferences")).json()
    assert [(p["name"], p["filters"]) for p in listed] == [("default", {"remote": "false"})]


async def test_a_parameter_the_feed_does_not_read_is_refused(client: AsyncClient) -> None:
    """Profile answers in particular: a saved search is never a form answer."""
    response = await client.put(
        "/search-preferences/default", json={"filters": {"work_auth": "citizen"}}
    )

    assert response.status_code == 400
    assert "not a feed filter: work_auth" in response.json()["error"]["message"]


async def test_a_malformed_scope_is_refused(client: AsyncClient) -> None:
    response = await client.get("/search-preferences?scope=everyone")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_a_missing_search_is_not_found(client: AsyncClient) -> None:
    response = await client.get("/search-preferences/nothing-here")

    assert response.status_code == 404


async def test_deleting_is_idempotent(client: AsyncClient) -> None:
    await client.put("/search-preferences/default", json={"filters": {"remote": "true"}})

    assert (await client.delete("/search-preferences/default")).status_code == 204
    assert (await client.delete("/search-preferences/default")).status_code == 204
    assert (await client.get("/search-preferences/default")).status_code == 404
