import uuid
from datetime import UTC, datetime

import pytest

from teamboard.domain import Plan, Role
from teamboard.errors import BadRequestError
from teamboard.pagination import Cursor
from tests.support import AdminDb, ApiDriver, assert_problem


def test_cursor_round_trips() -> None:
    cursor = Cursor(created_at=datetime(2030, 1, 2, 3, 4, 5, 678901, tzinfo=UTC), id=uuid.uuid4())

    assert Cursor.decode(cursor.encode()) == cursor
    assert "=" not in cursor.encode()  # URL-safe without padding


@pytest.mark.parametrize(
    "value",
    [
        "garbage",
        "bm90LWpzb24",  # base64("not-json")
        "eyJjIjogIjIwMzAtMDEtMDFUMDA6MDA6MDAiLCAiaSI6ICJ4In0",  # naive timestamp, bad id
        "e30",  # base64("{}")
        "W10",  # base64("[]")
    ],
)
def test_malformed_cursor_is_rejected(value: str) -> None:
    with pytest.raises(BadRequestError) as error:
        Cursor.decode(value)
    assert error.value.code == "invalid_cursor"


async def test_pages_cover_every_item_exactly_once_newest_first(api: ApiDriver) -> None:
    ws = await api.create_workspace(plan=Plan.PRO)
    created = [(await api.create_project(ws))["id"] for _ in range(25)]

    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 10}
        if cursor:
            params["cursor"] = cursor
        body = (await api.client.get("/v1/projects", params=params, headers=ws.headers())).json()
        pages.append([p["id"] for p in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert [len(page) for page in pages] == [10, 10, 5]
    assert [project_id for page in pages for project_id in page] == list(reversed(created))


async def test_last_page_has_no_cursor_even_when_exactly_full(api: ApiDriver) -> None:
    ws = await api.create_workspace()
    for _ in range(4):
        await api.create_project(ws)

    first = (await api.client.get("/v1/projects?limit=2", headers=ws.headers())).json()
    second = (
        await api.client.get(
            "/v1/projects",
            params={"limit": 2, "cursor": first["next_cursor"]},
            headers=ws.headers(),
        )
    ).json()

    assert first["next_cursor"] is not None
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None


async def test_items_created_while_paging_do_not_shift_later_pages(api: ApiDriver) -> None:
    """The advantage over OFFSET: an insert between two page requests neither
    duplicates nor skips items."""
    ws = await api.create_workspace(plan=Plan.PRO)
    original = [(await api.create_project(ws))["id"] for _ in range(6)]

    first = (await api.client.get("/v1/projects?limit=3", headers=ws.headers())).json()
    await api.create_project(ws)  # lands "before" page one in newest-first order
    second = (
        await api.client.get(
            "/v1/projects",
            params={"limit": 3, "cursor": first["next_cursor"]},
            headers=ws.headers(),
        )
    ).json()

    seen = [p["id"] for p in first["items"] + second["items"]]
    assert seen == list(reversed(original))


async def test_identical_timestamps_are_ordered_by_id(api: ApiDriver, db: AdminDb) -> None:
    """The cursor is (created_at, id): rows sharing a timestamp are still returned
    exactly once, in a stable order."""
    ws = await api.create_workspace(plan=Plan.PRO)
    ids = [(await api.create_project(ws))["id"] for _ in range(7)]
    await db.execute(
        "UPDATE projects SET created_at = '2030-01-01T00:00:00Z' WHERE tenant_id = :t",
        t=uuid.UUID(ws.tenant_id),
    )

    listed = [p["id"] for p in await api.collect("/v1/projects", ws.headers(), limit=2)]

    assert listed == sorted(ids, key=lambda value: uuid.UUID(value).int, reverse=True)


async def test_a_cursor_cannot_be_used_to_read_another_tenant(api: ApiDriver) -> None:
    """A cursor is only a position, not a capability."""
    mine, theirs = await api.create_workspace(), await api.create_workspace()
    for _ in range(3):
        await api.create_project(theirs)
    my_project = await api.create_project(mine)
    their_cursor = (await api.client.get("/v1/projects?limit=1", headers=theirs.headers())).json()[
        "next_cursor"
    ]
    far_future = Cursor(created_at=datetime(2999, 1, 1, tzinfo=UTC), id=uuid.uuid4()).encode()

    for cursor in (their_cursor, far_future):
        response = await api.client.get(
            "/v1/projects", params={"cursor": cursor}, headers=mine.headers()
        )
        assert {p["id"] for p in response.json()["items"]} <= {my_project["id"]}


@pytest.mark.parametrize("limit", [0, 101, -1, "many"])
async def test_limit_is_bounded(api: ApiDriver, limit: int | str) -> None:
    ws = await api.create_workspace()

    response = await api.client.get("/v1/projects", params={"limit": limit}, headers=ws.headers())

    body = assert_problem(response, 422, "validation_error")
    assert body["errors"][0]["field"] == "query.limit"


async def test_invalid_cursor_is_a_bad_request(api: ApiDriver) -> None:
    ws = await api.create_workspace()

    for path in ("/v1/projects", "/v1/tasks", "/v1/members", "/v1/audit-logs", "/v1/exports"):
        response = await api.client.get(path, params={"cursor": "!!!"}, headers=ws.headers())
        assert_problem(response, 400, "invalid_cursor")


async def test_every_list_endpoint_paginates(api: ApiDriver) -> None:
    ws = await api.create_workspace(plan=Plan.PRO)
    project = await api.create_project(ws)
    for index in range(3):
        await api.create_task(ws, project["id"])
        await api.invite(ws, f"invitee-{index}@example.com", Role.VIEWER)
        await api.create_api_key(ws, ["tasks:read"])
        await api.client.post("/v1/exports", headers=ws.headers())

    for path in (
        "/v1/tasks",
        "/v1/invitations",
        "/v1/api-keys",
        "/v1/exports",
        "/v1/audit-logs",
    ):
        first = (await api.client.get(path, params={"limit": 2}, headers=ws.headers())).json()
        everything = await api.collect(path, ws.headers(), limit=2)
        assert len(first["items"]) == 2, path
        assert first["next_cursor"] is not None, path
        assert len(everything) >= 3, path
        assert len({item["id"] for item in everything}) == len(everything), path
