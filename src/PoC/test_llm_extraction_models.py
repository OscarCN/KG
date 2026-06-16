"""
Compare extraction models interactively.

Pulls a few real document texts, runs the **full extraction pipeline**
(keyword match → LLM classification → per-supertype LLM extraction) under each
candidate model, and records the LLM wall-clock time and the extracted records
so you can eyeball quality and speed side by side.

Two things this script deliberately does so the comparison is fair:

* **Caching is disabled.** The extraction/classification cache keys on
  ``(article_url, class)`` only — *not* on the model — so without this every
  model after the first would just replay the first model's cached output (and
  report ~0s). We monkeypatch the cache read/write to no-ops for the session.
* **The model is injected per call.** ``extract.call_llm`` is wrapped to pass
  ``model=<current>`` to OpenRouter and to time every call, so classification
  and each extraction attempt are all measured.

Only documents that actually match the ontology (non-empty ``match``) are kept,
so every selected doc exercises the LLM.

Run:
    ELASTIC_HOST=localhost ipython src/PoC/test_llm_extraction_models.py
or, inside an IPython session:
    %run src/PoC/test_llm_extraction_models.py

Config (env vars, or edit the constants below):
    TEST_MODELS       comma list of OpenRouter model ids to compare
                      (default: current OPENROUTER_MODEL + a few alternatives)
    TEST_DATA_SUBDIR  data/<subdir> to pull documents from
                      (default: geo_qro_paid_mass_event)
    TEST_N_DOCS       how many matching docs to test (default: 3)
    TEST_DOC_IDS      optional comma list of ES ``_id`` to force specific docs

After it finishes, these names are bound for inspection:
    docs         raw fixture documents selected
    articles     the article dicts fed to the extractor (id, title, text, ...)
    results      results[model][source_id] = {records, n_records, llm_s, ...}
    timings      flat list of every LLM call, including the exact prompt
                 ``messages`` (system + user) and the raw ``response``
    summary_df   pandas DataFrame, one row per (model, doc)
    by_model     pandas DataFrame, aggregate per model
    prompts_df   pandas DataFrame, one row per LLM call (the prompt index)
    show(model, source_id=None)        pretty-print extracted records for a run
    show_prompt(call=None, *, model=None, source_id=None, phase=None)
                 print the exact prompt (system + user turns) and the response;
                 pick one call by its index, or filter by model/doc/phase.
                 phase is "classify" (the classification call) or "extract"
                 (a per-supertype extraction call).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from dotenv import load_dotenv

_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env.local")

import src.entities.extraction.extract as extract_mod  # noqa: E402
from src.entities.extraction.extract import EntityExtractor, Ontology  # noqa: E402
from src.llm.openrouter import call_openrouter  # noqa: E402


# -- Configuration ------------------------------------------------------------

_CURRENT_MODEL_ENV = os.environ.get("OPENROUTER_MODEL", "minimax/minimax-m2.7")

# Candidate models. Edit this list (or set TEST_MODELS) to whatever you want to
# compare. Unknown/invalid ids don't crash the run — their calls are recorded as
# failures so you can see them in the summary. ``google/gemini-2.5-flash-lite``
# is known-good here (it's the default linker model).
DEFAULT_MODELS = [
    _CURRENT_MODEL_ENV,
    "google/gemini-3.1-flash-lite",
    #"deepseek/deepseek-v4-flash",
    #"google/gemma-4-31b-it",
    #"qwen/qwen3.5-flash-02-23",
    #"qwen/qwen3.7-plus",
]

MODELS: List[str] = [
    m.strip()
    for m in (os.environ.get("TEST_MODELS") or ",".join(DEFAULT_MODELS)).split(",")
    if m.strip()
]
# De-dupe while preserving order (current model often equals a default).
MODELS = list(dict.fromkeys(MODELS))

DATA_SUBDIR = os.environ.get("TEST_DATA_SUBDIR", "geo_qro_paid_mass_event")
N_DOCS = int(os.environ.get("TEST_N_DOCS") or 3)
FORCED_IDS = [s.strip() for s in (os.environ.get("TEST_DOC_IDS") or "").split(",") if s.strip()]


# -- Disable caching so each model actually runs ------------------------------

extract_mod._cache_read = lambda *a, **k: None
extract_mod._cache_write = lambda *a, **k: None
extract_mod._classify_cache_read = lambda *a, **k: None
extract_mod._classify_cache_write = lambda *a, **k: None


# -- Wrap call_llm: inject the model under test, capture the prompt, and time it

_CURRENT_MODEL: Optional[str] = None
_CURRENT_SID: Optional[str] = None  # set per document in the run loop, tags each call

# One entry per LLM call. ``messages`` is the exact prompt sent (system + user,
# including any retry-hint turn the extractor appended), ``response`` is the raw
# model output. The lean timing view (``tdf``) is derived from this with the
# heavy columns dropped.
timings: List[Dict[str, Any]] = []


def _phase_of(messages: List[Dict[str, str]]) -> str:
    """Label a call as classification vs per-supertype extraction from its system prompt."""
    system = (messages[0].get("content", "") if messages else "") or ""
    return "classify" if "modelo de clasificación" in system else "extract"


def _timed_call_llm(messages: List[Dict[str, str]]) -> str:
    """Drop-in for ``extract.call_llm`` — injects the model, records the prompt, times it.

    Every classification and extraction attempt funnels through here, so
    ``timings`` captures the full per-call breakdown including the prompt
    ``messages`` and the raw ``response`` for manual inspection.
    """
    chars_in = sum(len(m.get("content", "") or "") for m in messages)
    phase = _phase_of(messages)
    t0 = time.perf_counter()
    try:
        out = call_openrouter(
            messages,
            model=_CURRENT_MODEL,
            response_format={"type": "json_object"},
        )
        dt = time.perf_counter() - t0
        timings.append({
            "model": _CURRENT_MODEL, "source_id": _CURRENT_SID, "phase": phase,
            "seconds": dt, "ok": True, "chars_in": chars_in,
            "chars_out": len(out or ""), "error": None,
            "messages": messages, "response": out,
        })
        return out
    except Exception as exc:  # noqa: BLE001 — record, then re-raise for retry logic
        dt = time.perf_counter() - t0
        timings.append({
            "model": _CURRENT_MODEL, "source_id": _CURRENT_SID, "phase": phase,
            "seconds": dt, "ok": False, "chars_in": chars_in,
            "chars_out": 0, "error": f"{type(exc).__name__}: {exc}",
            "messages": messages, "response": None,
        })
        raise


extract_mod.call_llm = _timed_call_llm


# -- Load documents -----------------------------------------------------------

def _to_article(record: dict) -> dict:
    """Map an ES news hit (flat fields) to the article dict the extractor wants."""
    body = record.get("text") or record.get("summary") or ""
    title = record.get("title") or ""
    url = record.get("url") or record.get("_id") or ""
    doc_type = str(record.get("doctype") or record.get("type") or "news").lower()
    publication_date = (
        record.get("article_date")
        or record.get("date_created")
        or record.get("date")
        or record.get("published_at")
    )
    categories: List[str] = []
    custom = record.get("custom_categories") or {}
    if isinstance(custom, dict):
        for level_vals in custom.values():
            if isinstance(level_vals, list):
                categories.extend(level_vals)
            elif isinstance(level_vals, str):
                categories.append(level_vals)
    return {
        "id": str(record.get("_id") or url),
        "text": body,
        "title": title,
        "url": url,
        "categories": categories,
        "document_type": doc_type,
        "publication_date": publication_date,
    }


def _load_fixture_records(subdir: str) -> List[dict]:
    data_dir = _PROJECT_ROOT / "data" / subdir
    files = sorted(data_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"No *.json fixtures under {data_dir}")
    records: List[dict] = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            payload = json.load(f)
        records.extend(payload if isinstance(payload, list) else [payload])
    return records


print(f"Models under test ({len(MODELS)}): {MODELS}")
print(f"Source: data/{DATA_SUBDIR}  |  want {N_DOCS} matching docs")

ontology = Ontology()
extractor = EntityExtractor(ontology=ontology)

raw_records = _load_fixture_records(DATA_SUBDIR)
by_id = {str(r.get("_id") or r.get("url")): r for r in raw_records}

docs: List[dict] = []
articles: List[dict] = []

if FORCED_IDS:
    for doc_id in FORCED_IDS:
        rec = by_id.get(doc_id)
        if rec is None:
            print(f"  ! forced id not found in fixture: {doc_id}")
            continue
        docs.append(rec)
        articles.append(_to_article(rec))
else:
    # Keep the first N docs that actually match the ontology, so each one
    # exercises classification + extraction (a non-matching doc does no LLM work).
    for rec in raw_records:
        if len(docs) >= N_DOCS:
            break
        art = _to_article(rec)
        if not (art["text"] or "").strip():
            continue
        if extractor.match(art):
            docs.append(rec)
            articles.append(art)

print(f"Selected {len(articles)} documents:")
for art in articles:
    matched = extractor.match(art)
    preview = (art["title"] or art["text"])[:70].replace("\n", " ")
    print(f"  {art['id']}  matched={len(matched)}  {preview!r}")


# -- Run every model over every document --------------------------------------

# results[model][source_id] = {...}
results: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)

print("\nRunning ...")
for model in MODELS:
    _CURRENT_MODEL = model
    print(f"\n=== model: {model} ===")
    for art in articles:
        sid = art["id"]
        _CURRENT_SID = sid
        start = len(timings)
        t0 = time.perf_counter()
        error: Optional[str] = None
        try:
            recs = extractor.extract(art, validate=True, raise_validation_error=False)
        except Exception as exc:  # noqa: BLE001
            recs = []
            error = f"{type(exc).__name__}: {exc}"
        wall = time.perf_counter() - t0
        calls = timings[start:]
        llm_s = sum(c["seconds"] for c in calls)
        ok_calls = sum(1 for c in calls if c["ok"])
        results[model][sid] = {
            "records": recs,
            "n_records": len(recs),
            "supertypes": dict(Counter(r.get("_supertype", "?") for r in recs)),
            "n_calls": len(calls),
            "ok_calls": ok_calls,
            "llm_s": llm_s,
            "wall_s": wall,
            "error": error,
        }
        flag = "" if error is None else "  ERROR"
        print(
            f"  {sid[:48]:48s} n_rec={len(recs):2d} "
            f"calls={ok_calls}/{len(calls)} llm={llm_s:6.1f}s{flag}"
        )

_CURRENT_MODEL = None
_CURRENT_SID = None


# -- Summaries ----------------------------------------------------------------

# Lean timing view: every numeric/label column from ``timings`` except the heavy
# ``messages`` / ``response`` payloads (kept in ``timings`` for show_prompt()).
_LEAN_COLS = ["model", "source_id", "phase", "seconds", "ok", "chars_in", "chars_out", "error"]

rows = []
for model in MODELS:
    for sid, r in results[model].items():
        rows.append({
            "model": model,
            "source_id": sid,
            "n_records": r["n_records"],
            "supertypes": r["supertypes"],
            "n_calls": r["n_calls"],
            "ok_calls": r["ok_calls"],
            "llm_s": round(r["llm_s"], 1),
            "wall_s": round(r["wall_s"], 1),
            "error": r["error"],
        })
summary_df = pd.DataFrame(rows)

tdf = pd.DataFrame(timings, columns=_LEAN_COLS) if timings else pd.DataFrame(columns=_LEAN_COLS)
# One row per LLM call — the index for picking a prompt to inspect.
prompts_df = tdf.reset_index().rename(columns={"index": "call"})[
    ["call", "model", "source_id", "phase", "seconds", "ok", "chars_in", "chars_out"]
]
by_model = (
    summary_df.groupby("model")
    .agg(
        docs=("source_id", "count"),
        total_records=("n_records", "sum"),
        total_llm_s=("llm_s", "sum"),
        mean_llm_s_per_doc=("llm_s", "mean"),
        errors=("error", lambda s: int(s.notna().sum())),
    )
    .round(1)
    .reindex(MODELS)
)
if not tdf.empty:
    call_stats = (
        tdf.groupby("model")
        .agg(
            calls=("seconds", "count"),
            ok=("ok", "sum"),
            mean_call_s=("seconds", "mean"),
            max_call_s=("seconds", "max"),
        )
        .round(1)
        .reindex(MODELS)
    )
    by_model = by_model.join(call_stats[["mean_call_s", "max_call_s"]])


def show(model: str, source_id: Optional[str] = None) -> None:
    """Pretty-print the extracted records for one model (and optionally one doc)."""
    per_doc = results.get(model)
    if not per_doc:
        print(f"No results for model {model!r}. Have: {list(results)}")
        return
    sids = [source_id] if source_id else list(per_doc)
    for sid in sids:
        r = per_doc.get(sid)
        if r is None:
            print(f"  (no result for {sid})")
            continue
        print(f"\n### {model}  |  {sid}  |  {r['n_records']} records  "
              f"|  llm={r['llm_s']:.1f}s  |  err={r['error']}")
        print(json.dumps(r["records"], ensure_ascii=False, indent=2, default=str))


def show_prompt(
    call: Optional[int] = None,
    *,
    model: Optional[str] = None,
    source_id: Optional[str] = None,
    phase: Optional[str] = None,
    response: bool = True,
) -> None:
    """Print the exact prompt (system + user turns) sent to the model.

    Pick a single call by its ``call`` index (see ``prompts_df``), or filter by
    ``model`` / ``source_id`` / ``phase`` ("classify" or "extract") to print all
    matching calls. With no arguments, prints every captured call.

    Examples:
        show_prompt(0)                       # the classification prompt of call 0
        show_prompt(phase="extract")         # every per-supertype extraction prompt
        show_prompt(model=MODELS[0], source_id=articles[0]["id"])
    """
    if call is not None:
        selected = [(call, timings[call])]
    else:
        selected = [
            (i, c) for i, c in enumerate(timings)
            if (model is None or c["model"] == model)
            and (source_id is None or c["source_id"] == source_id)
            and (phase is None or c["phase"] == phase)
        ]
    if not selected:
        print("No calls match. See prompts_df for the available calls.")
        return
    for i, c in selected:
        print("\n" + "=" * 100)
        print(
            f"call #{i}  |  phase={c['phase']}  |  model={c['model']}  |  "
            f"doc={c['source_id']}  |  {c['seconds']:.1f}s  |  ok={c['ok']}"
            + ("" if c["error"] is None else f"  |  {c['error']}")
        )
        for turn in c["messages"]:
            print(f"\n----- {turn.get('role', '?').upper()} -----")
            print(turn.get("content", ""))
        if response:
            print("\n----- RESPONSE -----")
            print(c["response"])


print("\n================ per (model, doc) ================")
with pd.option_context("display.max_colwidth", 40, "display.width", 160):
    print(summary_df.to_string(index=False))

print("\n================ per model ================")
print(by_model.to_string())

print("\n================ captured prompts (one row per LLM call) ================")
with pd.option_context("display.max_colwidth", 40, "display.width", 160):
    print(prompts_df.to_string(index=False))

print(
    "\nInspect results:  show('<model>'[, '<source_id>'])\n"
    "Inspect prompts:   show_prompt(<call#>)   |   show_prompt(phase='extract')   |\n"
    "                   show_prompt(model='<model>', source_id='<id>')\n"
    "Bound names: docs, articles, results, timings, summary_df, by_model, prompts_df,\n"
    "             show(), show_prompt()"
)
