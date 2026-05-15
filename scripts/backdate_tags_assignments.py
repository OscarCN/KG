"""Backdate existing tags assignments to each source's `date_created`.

One-off: rewrites `stance_assignments.assigned_at` and
`claim_assignments.extracted_at` for `(entity_id, org_id)` so existing
rows from prior wall-clock runs align with the article-date axis used
by `SIMULATE_ASSIGNED_AT_FROM_DOCUMENT=True` going forward.

Build a `source_item_id -> date_created` map from every fixture this
(entity, org) was streamed against (root URL and each comment_id both
resolve to the article's `date_created`), then `UPDATE … FROM (VALUES …)`
in two bulk statements. Rows whose `source_item_id` isn't in any fixture
are left untouched.

Usage:
    python scripts/backdate_tags_assignments.py        # dry run
    DRY_RUN=0 python scripts/backdate_tags_assignments.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env.local")

from psycopg2.extras import execute_values  # noqa: E402

from src.entities.tags.db import connect_userdb  # noqa: E402


# ── Knobs ───────────────────────────────────────────────────────────────

ENTITY_ID = 75
ORG_ID = 93

# Every corpus streamed under this (entity, org). Doc shape: list with
# `url`, `date_created`, optional `comments[].comment_id`. Both the
# pre-linked output and the raw news-fixture shape work.
FIXTURES = [
    _PROJECT_ROOT / "data" / "linked" / "ayuntamiento_qro_20260506_175946.json",
    _PROJECT_ROOT / "data" / "ayuntamiento_qro" / "ayuntamiento_qro_20260514_041941.json",
]

DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"


def build_simulated_now_map(fixtures: list[Path]) -> dict[str, str]:
    """source_item_id -> ISO date_created (parent date for comments)."""
    out: dict[str, str] = {}
    for path in fixtures:
        if not path.exists():
            print(f"  skip (missing): {path}")
            continue
        with open(path, encoding="utf-8") as f:
            docs = json.load(f)
        n_root = n_cmt = 0
        for doc in docs:
            url = doc.get("url")
            dc = doc.get("date_created")
            if not url or not dc:
                continue
            out.setdefault(url, dc)
            n_root += 1
            for c in doc.get("comments") or []:
                cid = c.get("comment_id")
                if not cid:
                    continue
                # Match the repo rule under SIMULATE_ASSIGNED_AT_FROM_DOCUMENT:
                # comment rows inherit the parent post's date_created.
                out.setdefault(str(cid), dc)
                n_cmt += 1
        print(f"  loaded {path.name}: {n_root} posts + {n_cmt} comments")
    return out


def update_table(cur, *, table: str, ts_column: str, pairs: list[tuple[str, str]]) -> int:
    sql = f"""
        UPDATE {table} AS t
           SET {ts_column} = v.new_at::timestamptz
          FROM (VALUES %s) AS v(source_item_id, new_at)
         WHERE t.entity_id = {int(ENTITY_ID)}
           AND t.org_id = {int(ORG_ID)}
           AND t.source_item_id = v.source_item_id
    """
    execute_values(cur, sql, pairs, page_size=500)
    return cur.rowcount


def main() -> int:
    print(f"scope: entity_id={ENTITY_ID} org_id={ORG_ID}  dry_run={DRY_RUN}")
    sim_now = build_simulated_now_map(FIXTURES)
    print(f"map: {len(sim_now)} source_item_ids resolved")
    if not sim_now:
        print("nothing to do")
        return 0
    pairs = list(sim_now.items())

    conn = connect_userdb()
    try:
        with conn.cursor() as cur:
            n_stance = update_table(
                cur, table="stance_assignments", ts_column="assigned_at", pairs=pairs,
            )
            n_claim = update_table(
                cur, table="claim_assignments", ts_column="extracted_at", pairs=pairs,
            )
        if DRY_RUN:
            conn.rollback()
            print(f"[dry run] would update stance_assignments={n_stance} "
                  f"claim_assignments={n_claim} — rolled back")
        else:
            conn.commit()
            print(f"committed: stance_assignments={n_stance} "
                  f"claim_assignments={n_claim}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
