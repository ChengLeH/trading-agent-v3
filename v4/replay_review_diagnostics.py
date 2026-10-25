"""Read-only, model-free measurement: python -m v4.replay_review_diagnostics CHECKPOINT.

Rebuilds requests from canonical objects. This measures context, not semantic
acceptance, and never promotes old verdicts to receipts under the new format.
"""
import argparse
from copy import deepcopy
import json
from statistics import median

from .agent_reviews import _item_wire_bytes
from .atomic_phase6 import build_atomic_tasks, build_atomic_prompt
from .checkpoint import load_checkpoint
from .financial_review import ingestion_diagnostics
from .review_diagnostics import diagnostics_index
from .review_budget import review_wire_breakdown, DEFAULT_ATOMIC_REVIEW_MAX_BYTES
from .semantic_contracts import semantic_review_schema


def measure(run):
    full = ingestion_diagnostics(run)
    items = []
    for task in build_atomic_tasks(run):
        schema = semantic_review_schema(task.claim)
        prompt = build_atomic_prompt(task, verification_epoch='offline-context-measurement')
        # An estimate using the current boilerplate, not a historical wire capture.
        legacy = deepcopy(task)
        legacy.context['evidence_scope']['ingestion_diagnostics'] = full
        old_prompt = build_atomic_prompt(legacy, verification_epoch='offline-context-measurement')
        local = task.context['evidence_scope']['ingestion_diagnostics']
        items.append(dict(claim_id=task.claim_id, **review_wire_breakdown(prompt, schema),
            full_embedding_estimate_bytes=_item_wire_bytes(old_prompt, schema),
            requires_scoped_review=local['requires_scoped_review'],
            omitted_count=local['truncation']['omitted_count']))
    sizes = [row['wire_bytes'] for row in items]
    return dict(mode='offline-measurement-only', semantic_verdicts_reverified=False,
        diagnostics=diagnostics_index(full), claim_count=len(items),
        wire_min=min(sizes, default=0), wire_median=median(sizes) if sizes else 0,
        wire_max=max(sizes, default=0), over_atomic_default=sum(size > DEFAULT_ATOMIC_REVIEW_MAX_BYTES for size in sizes),
        incomplete_diagnostics=sum(row['requires_scoped_review'] for row in items), items=items)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkpoint')
    args = parser.parse_args()
    runner = load_checkpoint(args.checkpoint, continue_verification=False)
    print(json.dumps(measure(runner.run), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
