"""Refresh only the public snapshot. A failed check leaves the last good version online."""

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

from snapshot import snapshot


def github(exe, endpoint, *, body=None):
    command = [exe, "api", endpoint]
    if body is not None:
        command.extend(["--method", "PUT", "--input", "-"])
    result = subprocess.run(
        command,
        capture_output=True,
        timeout=60,
        input=json.dumps(body).encode() if body is not None else None,
    )
    if result.returncode:
        raise RuntimeError("GitHub snapshot operation failed")
    return json.loads(result.stdout)


def refresh(ledger, repository, gh):
    endpoint = "repos/GiladR1979/gilad-markets/contents/docs/deals.json"
    previous = github(gh, endpoint + "?ref=main")
    old = json.loads(base64.b64decode(previous["content"]))
    new = snapshot(ledger, repository, gh)
    if new["checked"] < old["checked"]:
        raise ValueError("Publisher clock predates the published snapshot")
    changed = {k: v for k, v in new.items() if k != "checked"} != {
        k: v for k, v in old.items() if k != "checked"
    }
    if not changed and new["checked"] - old["checked"] < 6 * 3600:
        print("Checked. Published data is current.")
        return
    encoded = (
        json.dumps(new, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode()
    result = github(
        gh,
        endpoint,
        body={
            "message": "Refresh closed-market performance",
            "branch": "main",
            "sha": previous["sha"],
            "content": base64.b64encode(encoded).decode("ascii"),
        },
    )
    verified = github(gh, endpoint + "?ref=" + result["commit"]["sha"])
    if base64.b64decode(verified["content"]) != encoded:
        raise ValueError("Published snapshot verification failed")
    print("Verified published snapshot " + result["commit"]["sha"][:12])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--gh", default="gh")
    parser.add_argument("--tag-repo", required=True)
    args = parser.parse_args()
    try:
        refresh(args.ledger, args.tag_repo, args.gh)
    except Exception as exc:
        # HTTP exceptions may contain the request URL and account identifier.
        print("Snapshot refresh failed (" + type(exc).__name__ + ").", file=sys.stderr)
        sys.exit(1)
