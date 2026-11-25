import json
import sqlite3
import subprocess

from test_snapshot import leg

from tools.snapshot import build, includes_commit, period_entries, tag_periods


def deployment(at, sha):
    return {
        "activation_id": str(at),
        "manifest": {"source_commit": sha, "artifact_id": "test", "created_at": at - 1},
        "maintenance": {"created_at": at},
        "wallet": "fixture",
    }


def test_a_skipped_tagged_build_and_multiple_tags_in_one_rollout():
    history = [deployment(10, "old"), deployment(20, "child"), deployment(30, "later")]
    tags = [
        ("Successful8", "base"),
        ("Successful9", "next"),
        ("Successful10", "future"),
    ]
    parents = {("base", "child"), ("next", "child")}
    starts = tag_periods(history, tags, lambda base, head: (base, head) in parents)
    assert starts == {"Successful8": (20, "test"), "Successful9": (20, "test")}


def test_local_ancestry_distinguishes_descendants_from_older_commits(tmp_path):
    def git(*args):
        return (
            subprocess.check_output(["git", "-C", str(tmp_path), *args])
            .decode()
            .strip()
        )

    git("init", "--quiet")
    commits = []
    for name in ["tag", "child"]:
        git(
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Fixture",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            name,
        )
        commits.append(git("rev-parse", "HEAD"))
    base, child = commits
    assert includes_commit(base, child, "", "", tmp_path)
    assert not includes_commit(child, base, "", "", tmp_path)


def test_new_tag_keeps_prior_pairs_and_mixed_markets_out_of_new_period(tmp_path):
    ledger = tmp_path / "ledger.sqlite3"
    releases = [
        deployment(t, sha)
        for t, sha in [
            (10, "old"),
            (20, "eight"),
            (30, "followup"),
            (40, "nine"),
            (50, "more"),
        ]
    ]
    with sqlite3.connect(ledger) as db:
        db.executescript("""
            CREATE TABLE rn2_meta(key, body);
            CREATE TABLE rn2_release_history(activation_id, body);
            CREATE TABLE rn2_actions(action_id, prepared_at, action);
            CREATE TABLE rn2_orders(order_id, action_id, condition);
            CREATE TABLE rn2_executions(order_id, body);
            CREATE TABLE rn2_pairs(pair_id, body);
        """)
        for record in releases:
            db.execute(
                "INSERT INTO rn2_release_history VALUES (?,?)",
                (record["activation_id"], json.dumps(record)),
            )
        db.execute(
            "INSERT INTO rn2_meta VALUES ('api_first_rewrite_activation',?)",
            (json.dumps(releases[-1]),),
        )
        for index, (market, opened, prepared) in enumerate(
            [
                ("carry", 19, 31),
                ("tag", 20, 21),
                ("follow", 32, 33),
                ("latefill", 39, 45),
                ("next", 40, 41),
                ("nextfollow", 50, 51),
                ("mixed", 32, 33),
                ("mixed", 40, 42),
            ]
        ):
            pair = str(index)
            data = {"kind": "enter_pair", "condition": market}
            db.execute(
                "INSERT INTO rn2_actions VALUES (?,?,?)",
                (pair, prepared, json.dumps(data)),
            )
            db.execute("INSERT INTO rn2_orders VALUES (?,?,?)", (pair, pair, market))
            db.execute(
                "INSERT INTO rn2_pairs VALUES (?,?)",
                (
                    pair,
                    json.dumps(
                        {
                            "pair_id": pair,
                            "created_at": opened,
                            "condition": market,
                            "tokens": [market],
                        }
                    ),
                ),
            )
            db.execute(
                "INSERT INTO rn2_executions VALUES (?,?)",
                (
                    pair,
                    json.dumps(
                        {
                            "side": "BUY",
                            "condition": market,
                            "token": market,
                            "quantity": 5_000_000,
                        }
                    ),
                ),
            )

    tags = [("Successful8", "eight"), ("Successful9", "nine")]
    _, sizes, periods, bought = period_entries(ledger, tags)
    assert {k: v[0] for k, v in periods.items()} == {
        "Successful8": 20,
        "Successful9": 40,
    }
    rows = [
        dict(leg(m, m, -1, entered=at, bought=10 if m == "mixed" else 5), title=m)
        for m, at in [
            ("carry", 19),
            ("tag", 20),
            ("follow", 32),
            ("latefill", 45),
            ("next", 40),
            ("nextfollow", 50),
            ("mixed", 32),
        ]
    ]
    deals = build(
        rows,
        "nine",
        40,
        sizes,
        bought["Successful9"],
        200,
        tag="Successful9",
        prior_periods={"Successful8": (20, bought["Successful8"])},
    )["deals"]
    assert {r["market"] for r in deals if r["method"]} == {"next", "nextfollow"}
    assert {r["market"] for r in deals if r.get("releaseTag") == "Successful8"} == {
        "tag",
        "follow",
        "latefill",
    }
    assert all(r["verified"] for r in deals)
    assert {r["market"] for r in deals if "releaseTag" not in r} == {"carry", "mixed"}
