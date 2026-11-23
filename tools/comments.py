"""Build the public comment feed from the owner's approvals in dashboard issue #1."""

from datetime import datetime


REACTIONS = """
  nodes { user { id } createdAt }
  pageInfo { hasNextPage endCursor }
"""
THREAD = """
query($cursor: String) {
  repository(owner: "GiladR1979", name: "gilad-markets") {
    owner { id }
    issue(number: 1) {
      comments(first: 50, after: $cursor) {
        nodes {
          id author { login } bodyText createdAt lastEditedAt isMinimized
          reactions(content: THUMBS_UP, first: 20) { REACTIONS }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
""".replace("REACTIONS", REACTIONS)
MORE_REACTIONS = """
query($id: ID!, $cursor: String!) {
  node(id: $id) {
    ... on IssueComment {
      reactions(content: THUMBS_UP, first: 100, after: $cursor) { REACTIONS }
    }
  }
}
""".replace("REACTIONS", REACTIONS)


def pages(first, fetch_next):
    page = first
    seen = set()
    while True:
        yield from page["nodes"]
        info = page["pageInfo"]
        if not info["hasNextPage"]:
            return
        cursor = info["endCursor"]
        if not cursor or cursor in seen:
            raise ValueError("Invalid comment pagination")
        seen.add(cursor)
        page = fetch_next(cursor)


def approved_comments(query):
    def repository(cursor):
        return query(THREAD, {"cursor": cursor})["repository"]

    repo = repository(None)
    owner = repo["owner"]["id"]
    comments = pages(
        repo["issue"]["comments"], lambda c: repository(c)["issue"]["comments"]
    )
    feed = []
    for comment in comments:
        text = comment["bodyText"].strip()
        if comment["isMinimized"] or not comment["author"] or not text:
            continue

        def more(cursor):
            node = query(MORE_REACTIONS, {"id": comment["id"], "cursor": cursor})[
                "node"
            ]
            return node["reactions"]

        edited = comment["lastEditedAt"]
        for vote in pages(comment["reactions"], more):
            if not vote["user"] or vote["user"]["id"] != owner:
                continue
            # GitHub times have second precision. A same-second edit needs a new vote.
            if not edited or datetime.fromisoformat(
                vote["createdAt"]
            ) > datetime.fromisoformat(edited):
                feed.append(
                    {
                        "author": comment["author"]["login"],
                        "created": comment["createdAt"],
                        "text": text,
                    }
                )
            break
    return sorted(feed, key=lambda row: row["created"], reverse=True)
