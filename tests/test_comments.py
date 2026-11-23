from tools.comments import approved_comments
from tools import publish
import base64
import json


def page(nodes, cursor=None):
    return {
        "nodes": nodes,
        "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor},
    }


CREATED = "2026-11-20T10:00:00Z"
APPROVED = "2026-11-20T11:00:00Z"
EDITED = "2026-11-20T12:00:00Z"


def vote(user="owner", at=APPROVED):
    return {"user": {"id": user}, "createdAt": at}


def reply(text, reactions, **fields):
    return {
        "id": text,
        "author": {"login": "visitor"},
        "createdAt": CREATED,
        "bodyText": text,
        "lastEditedAt": None,
        "isMinimized": False,
        "reactions": page(reactions),
        **fields,
    }


def test_feed_only_includes_visible_current_text_approved_by_owner():
    rows = [
        reply("approved", [vote()]),
        reply("owner approval removed", [vote("other")]),
        reply("waiting", []),
        reply("hidden", [vote()], isMinimized=True),
        reply("edited later", [vote()], lastEditedAt=EDITED),
        reply("same-second edit", [vote()], lastEditedAt=APPROVED),
        reply("reviewed again", [vote(at=EDITED)], lastEditedAt=APPROVED),
        reply("deleted author", [vote()], author=None),
    ]

    def query(_, variables):
        assert variables == {"cursor": None}
        return {
            "repository": {"owner": {"id": "owner"}, "issue": {"comments": page(rows)}}
        }

    assert approved_comments(query) == [
        {"author": "visitor", "created": CREATED, "text": "approved"},
        {"author": "visitor", "created": CREATED, "text": "reviewed again"},
    ]


def test_all_pages_considered_and_newest_comments_come_first():
    first = reply("older", [])
    first["reactions"] = page([vote("guest")], "reactions-2")
    newer = reply("newer", [vote(at=EDITED)], createdAt=APPROVED)

    def query(_, variables):
        cursor = variables["cursor"]
        if variables.get("id"):
            assert cursor == "reactions-2" and variables["id"] == "older"
            return {"node": {"reactions": page([vote()])}}
        comments = page([first], "comments-2") if cursor is None else page([newer])
        return {
            "repository": {"owner": {"id": "owner"}, "issue": {"comments": comments}}
        }

    assert [r["text"] for r in approved_comments(query)] == ["newer", "older"]


def test_publisher_adds_and_withdraws_without_new_trade_or_duplicate_writes(
    monkeypatch,
):
    public_file = {"encoding": "base64", "content": "W10K", "sha": "base"}
    writes = []
    rows = [reply("ready", [vote()])]

    def github(gh, endpoint, *, body=None, method="PUT"):
        if endpoint == "graphql":
            assert method == "POST"
            return {
                "data": {
                    "repository": {
                        "owner": {"id": "owner"},
                        "issue": {"comments": page(rows)},
                    }
                }
            }
        assert endpoint.startswith(publish.PUBLIC_REPO + "/contents/docs/comments.json")
        if body is None:
            return public_file.copy()
        assert body["sha"] == public_file["sha"]
        writes.append(json.loads(base64.b64decode(body["content"])))
        public_file.update(content=body["content"], sha=str(len(writes)))
        return {"commit": {"sha": public_file["sha"]}}

    monkeypatch.setattr(publish, "github", github)
    publish.refresh_comments("gh")
    publish.refresh_comments("gh")
    rows[0]["reactions"] = page([])
    publish.refresh_comments("gh")
    assert writes == [[{"author": "visitor", "text": "ready", "created": CREATED}], []]
