"""Run the reconciliation sweep and apply the merges to kgdb (direction B).

Composes the read-only pipeline in ``scripts/reconcile_dryrun.py`` (retrieval →
units → LLM unit-cluster+layer → sibling guard) into a **merge plan**, then feeds
each multi-entity group to ``CanonicalMerger`` (``linking/merge.py``).

Two modes, **dry-run by default**:
    python scripts/reconcile_apply.py                     # plan only, print, no writes
    RECON_LLM=1 python scripts/reconcile_apply.py         # + LLM adjudication (still dry)
    RECON_APPLY=1 python scripts/reconcile_apply.py       # EXECUTE the merges (dev kgdb)
    RECON_SUPERTYPE=paid_mass_event RECON_APPLY=1 ...      # scope to one supertype

Writes go to the KGDB_* connection (``.env.local`` → dev :5334). Every merge is
appended to ``data/.runlogs/reconcile_apply.jsonl`` as an audit trail.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.reconcile_dryrun import (  # noqa: E402
    USE_LLM, _connect, _load_events, merge_plan, run_pipeline,
)
from src.entities.linking.merge import CanonicalMerger  # noqa: E402

_AUDIT = _PROJECT_ROOT / "data" / ".runlogs" / "reconcile_apply.jsonl"
_REVIEW = _PROJECT_ROOT / "data" / ".runlogs" / "reconcile_review.txt"


def main() -> None:
    supertype = os.environ.get("RECON_SUPERTYPE")
    apply = os.environ.get("RECON_APPLY") == "1"

    conn = _connect()
    events = _load_events(conn, supertype)
    print(f"loaded {len(events)} canonical events"
          + (f" (supertype={supertype})" if supertype else " (all supertypes)"))

    final_events = run_pipeline(events, USE_LLM)
    plan = merge_plan(events, final_events)
    print(f"\nmerge plan: {len(plan)} groups, "
          f"collapsing {sum(len(g['entity_ids']) for g in plan)} canonicals")

    by_id = {e["id"]: e for e in events}
    merger = CanonicalMerger(conn)
    _AUDIT.parent.mkdir(parents=True, exist_ok=True)
    _REVIEW.parent.mkdir(parents=True, exist_ok=True)
    review = open(_REVIEW, "a" if apply else "w", encoding="utf-8")
    done = 0
    for g in sorted(plan, key=lambda g: -len(g["entity_ids"])):
        try:
            summary = merger.merge(g["entity_ids"], g["layer"], dry_run=not apply)
        except Exception as ex:  # noqa: BLE001
            conn.rollback()
            print(f"  ERROR on {g['entity_ids']}: {ex}")
            continue
        surv, absorbed = summary["survivor"], summary["absorbed"]
        st = by_id[surv]["supertype"]
        drop = f" drop={summary['outliers_dropped']}" if summary["outliers_dropped"] else ""
        header = (f"[{summary['layer'].upper():<8}] {st}  survivor {surv} "
                  f"{summary['date']['start']}..{summary['date']['end']}{drop}")
        review.write(header + "\n")

        def _line(eid, mark):
            e = by_id.get(eid, {})
            d = e.get("start")
            ds = d.date().isoformat() if hasattr(d, "date") else "?"
            return (f"    {mark} id={eid:<6} src={e.get('n_sources','?'):>3} "
                    f"{(e.get('event_type') or '?'):<14} {ds}  {(e.get('name') or '')[:60]!r}")
        review.write(_line(surv, "★") + "\n")
        for eid in absorbed:
            review.write(_line(eid, "·") + "\n")
        review.write("\n")

        tag = "MERGED" if apply else "would merge"
        print(f"  {header}  [{tag} {len(g['entity_ids'])}]")
        if apply:
            with open(_AUDIT, "a", encoding="utf-8") as f:
                f.write(json.dumps(summary, default=str, ensure_ascii=False) + "\n")
            done += 1
    review.close()
    conn.close()
    print(f"\n{'applied' if apply else 'dry-run'}: {done if apply else len(plan)} "
          f"merge group(s){' executed' if apply else ' (no writes — set RECON_APPLY=1)'}")
    print(f"review log (skim this): {_REVIEW}")


if __name__ == "__main__":
    main()
