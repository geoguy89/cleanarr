"""What has actually been muted, so the word lists can be judged on evidence.

Every word Cleanarr has silenced across the library, with how often, in how
many episodes, and where to find examples. The point is to answer "is this
word worth muting, always or never?" by looking rather than guessing.

Run on Tower:
    docker exec cleanarr python3 /config/word_report.py
    docker exec cleanarr python3 /config/word_report.py --word hell
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from collections import defaultdict

DB = os.environ.get("CLEANARR_DB", "/config/cleanarr.sqlite")


def normalise(text: str) -> str:
    return re.sub(r"[^a-z' ]", "", str(text or "").lower()).strip()


def stamp(seconds: float) -> str:
    total = int(seconds or 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--word", help="show every occurrence of one word")
    parser.add_argument("--min", type=int, default=1, help="hide words seen fewer times")
    args = parser.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT d.text, d.category, d.muted, d.review, d.reason, d.start,
               j.title, j.subtitle
        FROM detection d JOIN job j ON j.id = d.job_id
        WHERE j.status IN ('done','skipped')
        ORDER BY d.start
    """).fetchall()

    if args.word:
        wanted = normalise(args.word)
        hits = [r for r in rows if normalise(r["text"]) == wanted]
        print(f'"{args.word}" — {len(hits)} occurrence(s)\n')
        for r in hits:
            state = "muted" if r["muted"] else "left in"
            print(f"  {r['title']} {r['subtitle'][:26]:28s} {stamp(r['start'])}  "
                  f"{state}{'  ' + r['reason'] if r['reason'] else ''}")
        return

    per: dict[str, dict] = defaultdict(
        lambda: {"muted": 0, "left": 0, "category": "", "episodes": set(), "examples": []})
    for r in rows:
        word = normalise(r["text"])
        if not word:
            continue
        entry = per[word]
        entry["category"] = r["category"]
        entry["episodes"].add(f"{r['title']} {r['subtitle']}")
        if r["muted"]:
            entry["muted"] += 1
        else:
            entry["left"] += 1
        if len(entry["examples"]) < 3:
            entry["examples"].append(
                f"{r['title']} {r['subtitle'][:20]} @{stamp(r['start'])}")

    print(f"{'word':18s} {'muted':>6s} {'left in':>8s} {'episodes':>9s}  category")
    print("-" * 78)
    for word, e in sorted(per.items(), key=lambda kv: -kv[1]["muted"]):
        if e["muted"] + e["left"] < args.min:
            continue
        print(f"{word:18s} {e['muted']:6d} {e['left']:8d} {len(e['episodes']):9d}  "
              f"{e['category']}")
    total = sum(e["muted"] for e in per.values())
    print("-" * 78)
    print(f"{len(per)} distinct words, {total} mutes across "
          f"{len({ep for e in per.values() for ep in e['episodes']})} files")
    print("\nTo see where one word was used:  --word hell")


if __name__ == "__main__":
    main()
