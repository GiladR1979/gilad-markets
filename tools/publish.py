"""Refresh the performance snapshot and the separately moderated comment feed."""

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

if __package__:
    from .comments import approved_comments
    from .snapshot import snapshot
else:
    from comments import approved_comments
    from snapshot import snapshot


PUBLIC_REPO = "repos/GiladR1979/gilad-markets"


def github(exe, endpoint, *, body=None, method="PUT"):
    command = [exe, "api", endpoint]
    if body is not None:
        command.extend(["--method", method, "--input", "-"])
    result = subprocess.run(
        command,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=60,
        input=json.dumps(body).encode() if body is not None else None,
    )
    if result.returncode:
        raise RuntimeError("GitHub snapshot operation failed")
    return json.loads(result.stdout)


def snapshot_bytes(gh, metadata):
    # The contents API omits inline data once the history grows beyond 1 MiB.
    if metadata["encoding"] == "none":
        metadata = github(gh, PUBLIC_REPO + "/git/blobs/" + metadata["sha"])
    if metadata["encoding"] != "base64":
        raise ValueError("Unsupported published snapshot encoding")
    return base64.b64decode(metadata["content"])


def refresh(ledger, repository, gh):
    endpoint = PUBLIC_REPO + "/contents/docs/deals.json"
    previous = github(gh, endpoint + "?ref=main")
    old = json.loads(snapshot_bytes(gh, previous))
    new = snapshot(ledger, repository, gh)
    if new["checked"] < old["checked"]:
        raise ValueError("Publisher clock predates the published snapshot")
    changed = {k: v for k, v in new.items() if k != "checked"} != {
        k: v for k, v in old.items() if k != "checked"
    }
    if not changed and new["checked"] - old["checked"] < 6 * 3600:
        print("Checked. Published data is current.")
        return
    publish_json(gh, endpoint, previous, new, "Refresh closed-market performance")


def publish_json(gh, endpoint, previous, data, message):
    encoded = (
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    result = github(
        gh,
        endpoint,
        body={
            "message": message,
            "branch": "main",
            "sha": previous["sha"],
            "content": base64.b64encode(encoded).decode("ascii"),
        },
    )
    verified = github(gh, endpoint + "?ref=" + result["commit"]["sha"])
    if snapshot_bytes(gh, verified) != encoded:
        raise ValueError("Published snapshot verification failed")
    print("Verified published snapshot " + result["commit"]["sha"][:12])


def refresh_comments(gh):
    endpoint = PUBLIC_REPO + "/contents/docs/comments.json"
    previous = github(gh, endpoint + "?ref=main")

    def query(document, variables):
        response = github(
            gh,
            "graphql",
            method="POST",
            body={"query": document, "variables": variables},
        )
        if response.get("errors"):
            raise RuntimeError("GitHub could not verify comment approvals")
        return response["data"]

    comments = approved_comments(query)
    if comments != json.loads(snapshot_bytes(gh, previous)):
        publish_json(
            gh, endpoint, previous, comments, "Update moderated dashboard comments"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--tag-repo", required=True)
    args = parser.parse_args()
    failed = False
    try:
        refresh_comments(args.gh)
    except Exception as exc:
        print("Comment refresh failed (" + type(exc).__name__ + ").", file=sys.stderr)
        failed = True
    try:
        refresh(args.ledger, args.tag_repo, args.gh)
    except Exception as exc:
        # HTTP exceptions may contain the request URL and account identifier.
        print("Snapshot refresh failed (" + type(exc).__name__ + ").", file=sys.stderr)
        failed = True
    sys.exit(int(failed))
