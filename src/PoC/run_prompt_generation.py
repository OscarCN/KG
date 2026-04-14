"""
Test the prompt generation pipeline step by step.

Designed to be run step-by-step in IPython:
    ipython src/PoC/run_prompt_generation.py

Or interactively in a Jupyter/IPython session using %run:
    %run src/PoC/run_prompt_generation.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv('.env.local')

# Ensure project root is on sys.path
#_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = Path('/Users/oscarcuellar/ocn/media/kg/kg/')
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ── Logging setup ─────────────────────────────────────────────────────────────
# Set DEBUG to see full prompts sent/received at each LLM step.
# Set INFO to see progress without the full prompt text.

#LOG_LEVEL = logging.DEBUG
LOG_LEVEL = logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
# Keep third-party loggers quiet
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

from src.entities.extraction.prompt_generator import (
    PromptGeneration,
    PromptGenerationContextManager,
    _get_available_supertypes,
)

# ── Configuration ─────────────────────────────────────────────────────────────

# Which supertypes to generate. Set to a list of names, or None to generate all missing.
SUPERTYPES = None  # e.g. ["protest_event", "arrest_event"] or None for all missing

# Set to True to only inspect the context (no LLM calls)
CONTEXT_ONLY: bool = False

# ── Step 1: Determine which supertypes to generate ───────────────────────────

_PROMPTS_DIR = _PROJECT_ROOT / "src" / "entities" / "extraction" / "prompts" / "classes"

all_supertypes = _get_available_supertypes()
existing_prompts = {p.stem for p in _PROMPTS_DIR.glob("*.txt")} if _PROMPTS_DIR.exists() else set()

if SUPERTYPES is not None:
    targets = SUPERTYPES
else:
    targets = [st for st in all_supertypes if st not in existing_prompts]

print("All supertypes:")
for st in all_supertypes:
    status = "EXISTS" if st in existing_prompts else "MISSING"
    selected = " << WILL GENERATE" if st in targets else ""
    print(f"  {st:30s} [{status}]{selected}")

if not targets:
    print("\nAll prompts already exist. Nothing to generate.")
    sys.exit(0)

print(f"\nWill generate {len(targets)} prompt(s): {', '.join(targets)}")

# ── Step 2: Loop over targets ────────────────────────────────────────────────

gen = None if CONTEXT_ONLY else PromptGeneration()

for i, SUPERTYPE in enumerate(targets, 1):
    print(f"\n{'='*70}")
    print(f"[{i}/{len(targets)}] {SUPERTYPE}")
    print(f"{'='*70}")

    # ── Build context ────────────────────────────────────────────────────────

    ctx_mgr = PromptGenerationContextManager(SUPERTYPE)
    context = ctx_mgr.to_dict()

    print(f"\nSchema key: {context['schema_key']}")
    print(f"Meta description: {context['meta_description'][:120]}...")
    print(f"\nFields ({len(context['fields'])}):")
    for field in context["fields"]:
        type_str = field["type"]
        req = " [REQUIRED]" if field.get("required") else ""
        enum_str = f" enum={field['enum']}" if field.get("enum") else ""
        print(f"  {field['name']:30s} {type_str:30s}{req}{enum_str}")

    print(f"\nComposite types referenced ({len(context['composite_types'])}):")
    for type_name, comp in context["composite_types"].items():
        fields_str = ", ".join(f["name"] for f in comp["fields"])
        print(f"  {type_name}: [{fields_str}]")

    print(f"\nMeta example:")
    print(json.dumps(context["meta_example"], indent=2, ensure_ascii=False)[:500])

    context_json = ctx_mgr.to_json()
    print(f"\nFull context JSON: {len(context_json)} chars")

    # ── Generate prompt (requires API key) ───────────────────────────────────

    if CONTEXT_ONLY:
        print("\nCONTEXT_ONLY=True — skipping LLM calls.")
        continue

    print(f"\nGeneration model: {gen.generation_model}")
    print(f"Feedback model:   {gen.feedback_model}")

    # Step a: Generate draft
    print("\n--- Generating draft...")
    draft = gen._generate_draft(context_json)
    print(f"Draft length: {len(draft)} chars")
    print(f"Draft preview (first 300 chars):\n{draft[:300]}")

    # Step b: Get feedback
    print("\n--- Getting feedback...")
    feedback = gen._get_feedback(draft, context_json)
    print(f"Feedback:\n{feedback}")

    # Step c: Apply feedback (or skip if no issues)
    if "NO ISSUES FOUND" in feedback.upper():
        final = draft
        print("\n--- No issues found, using draft as final.")
    else:
        print("\n--- Applying feedback...")
        final = gen._apply_feedback(draft, feedback, context_json)
        print(f"Final length: {len(final)} chars")

    # Step d: Validate
    warnings = gen._validate_prompt(final)
    if warnings:
        print(f"\nValidation warnings:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\nValidation: all checks passed")

    # Step e: Save
    gen._save_prompt(SUPERTYPE, final)
    print(f"\nPrompt saved to prompts/classes/{SUPERTYPE}.txt")

    print(f"\n--- Generated prompt for {SUPERTYPE} ---")
    print(final[:200] + "..." if len(final) > 200 else final)

print(f"\n{'='*70}")
print(f"Done. Generated {len(targets)} prompt(s).")
print(f"{'='*70}")
