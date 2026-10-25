"""Lossless binding-dimension references in review wire payloads."""
from copy import deepcopy
import json
import unittest

from .review_budget import (DEFAULT_ATOMIC_REVIEW_MAX_BYTES,
                            DEFAULT_DEEP_REVIEW_MAX_BYTES,
                            review_wire_breakdown,
                            requires_scoped_review)
from .atomic_phase6 import AtomicTask, build_atomic_prompt
from .review_batches import pack_review_requests
from .review_wire import (BINDING_DIMENSION_INSTRUCTIONS,
                          CONTEXT_CATALOG_INSTRUCTIONS,
                          decode_review_payload,
                          project_review_payload,
                          review_wire_instructions)


def dimensions(suffix=""):
    return {
        key: f"{key}-" + "x" * 40 + suffix
        for key in ("entity", "concept", "period", "scope", "unit", "basis")
    }


def claim(claim_id, dimension_rows):
    return {
        "claim_id": claim_id,
        "text": f"Claim {claim_id} remains verbatim.",
        "bindings": [
            {"ref_id": f"source.{claim_id}.{index}", **deepcopy(row)}
            for index, row in enumerate(dimension_rows)
        ],
    }


def json_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class ReviewWireProjection(unittest.TestCase):
    def test_projection_is_lossless_and_does_not_mutate_the_input(self):
        shared = dimensions()
        payload = {
            "tasks": [{
                "claim": claim(f"task.{index}", [shared]),
                "context": {"evidence": [f"unique evidence {index}"]},
            } for index in range(8)],
            "dependency_catalog": {"parent": claim("parent", [shared])},
            "verification_input_hash": "canonical-hash-must-stay-outside-the-projector",
        }
        payload["tasks"][0]["claim"]["bindings"][0]["extra"] = {
            "kept": ["binding metadata", {"line": 7}],
        }
        original = deepcopy(payload)

        projected = project_review_payload(payload)

        self.assertEqual(payload, original)
        self.assertIn("binding_dimension_catalog", projected)
        self.assertNotEqual(projected, payload)
        self.assertEqual(decode_review_payload(projected), payload)
        self.assertEqual(projected["verification_input_hash"], payload["verification_input_hash"])
        self.assertEqual(projected["tasks"][0]["context"], payload["tasks"][0]["context"])
        binding = projected["tasks"][0]["claim"]["bindings"][0]
        self.assertEqual(binding["ref_id"], "source.task.0.0")
        self.assertIn("binding_dimension_ref", binding)
        self.assertIn("binding_dimension_catalog", BINDING_DIMENSION_INSTRUCTIONS)

    def test_identical_context_fields_are_pooled_only_for_rows_that_reference_them(self):
        context = {"evidence_scope": {"counts": {"facts": 4}},
                   "large_shared_note": "相同 context 欄位只保留一份" * 40}
        payload = {"tasks": [
            {"claim": claim(f"claim.{index}", []), "context": deepcopy(context)}
            for index in range(8)
        ]}

        projected = project_review_payload(payload)

        self.assertIn("context_value_catalog", projected)
        self.assertIn(CONTEXT_CATALOG_INSTRUCTIONS, review_wire_instructions(projected))
        self.assertTrue(all("context_refs" in row
                            and "large_shared_note" not in row["context"]
                            for row in projected["tasks"]))
        self.assertEqual(decode_review_payload(projected), payload)

    def test_shared_structured_context_field_is_pooled_while_unique_fields_stay_inline(self):
        shared_scope = {"counts": {"facts": 10}, "inventory": "x" * 1000}
        payload = {"tasks": [
            {"claim": claim("a", []), "context": {
                "evidence_scope": deepcopy(shared_scope),
                "calculation_definitions": {"claim.a": {"operation": "sum"}},
            }},
            {"claim": claim("b", []), "context": {
                "evidence_scope": deepcopy(shared_scope),
                "calculation_definitions": {"claim.b": {"operation": "ratio"}},
            }},
        ]}

        projected = project_review_payload(payload)

        self.assertEqual(projected["context_value_catalog"]["c0"], shared_scope)
        self.assertEqual(projected["tasks"][0]["context"], {
            "calculation_definitions": {"claim.a": {"operation": "sum"}},
        })
        self.assertEqual(projected["tasks"][1]["context"], {
            "calculation_definitions": {"claim.b": {"operation": "ratio"}},
        })
        self.assertEqual(projected["tasks"][0]["context_refs"], {"evidence_scope": "c0"})
        self.assertEqual(projected["tasks"][1]["context_refs"], {"evidence_scope": "c0"})
        self.assertEqual(decode_review_payload(projected), payload)

    def test_projection_keeps_original_when_catalog_would_not_save_bytes(self):
        payload = {"task": {"claim": claim("a", [dimensions()])}}

        projected = project_review_payload(payload)

        self.assertEqual(projected, payload)
        self.assertNotIn("binding_dimension_catalog", projected)
        self.assertEqual(decode_review_payload(projected), payload)

    def test_misleading_bindings_inside_raw_evidence_are_never_rewritten(self):
        shared = dimensions()
        raw_evidence = {"bindings": [
            {"ref_id": f"raw.{index}", **deepcopy(shared)} for index in range(8)
        ], "content": "raw evidence remains byte-identical"}
        payload = {
            "task": {
                "claim": claim("root", [shared]),
                "dependency_claims": [claim("parent", [shared] * 7)],
                "resolved_refs": {"evidence.raw": raw_evidence},
                "context": {"unique": "task context"},
            },
            "evidence_catalog": {"evidence.raw": deepcopy(raw_evidence)},
        }
        original = deepcopy(payload)

        projected = project_review_payload(payload)

        self.assertIn("binding_dimension_catalog", projected)
        self.assertEqual(projected["task"]["resolved_refs"], original["task"]["resolved_refs"])
        self.assertEqual(projected["evidence_catalog"], original["evidence_catalog"])
        self.assertEqual(decode_review_payload(projected), original)

    def test_catalog_ids_and_order_are_stable_without_reordering_claims(self):
        shared = dimensions()
        reordered = dict(reversed(list(shared.items())))
        first = {"tasks": [
            {"claim": claim("b", [reordered])},
            {"claim": claim("a", [shared])},
            {"claim": claim("c", [dimensions("-other")])},
            {"claim": claim("d", [shared])},
            {"claim": claim("e", [shared])},
        ]}
        second = json.loads(json.dumps(first, ensure_ascii=False))
        second["tasks"] = list(reversed(second["tasks"]))

        projected_a = project_review_payload(first)
        projected_b = project_review_payload(second)

        self.assertEqual(list(projected_a["binding_dimension_catalog"]),
                         sorted(projected_a["binding_dimension_catalog"]))
        self.assertEqual(projected_a["binding_dimension_catalog"],
                         projected_b["binding_dimension_catalog"])
        self.assertNotIn("binding_dimension_ref",
                         projected_a["tasks"][2]["claim"]["bindings"][0])
        self.assertEqual([row["claim"]["claim_id"] for row in projected_a["tasks"]],
                         ["b", "a", "c", "d", "e"])
        self.assertEqual([row["claim"]["claim_id"] for row in projected_b["tasks"]],
                         ["e", "d", "c", "a", "b"])
        self.assertEqual(decode_review_payload(projected_a), first)
        self.assertEqual(decode_review_payload(projected_b), second)

    def test_atomic_and_batched_prompt_builders_use_the_same_projection(self):
        shared = dimensions()
        atomic_claim = claim("atomic", [shared] * 8)
        atomic_claim.update(refs=[], fact_refs=[], dependencies=[])
        atomic_task = AtomicTask("atomic", atomic_claim, {}, [], [], context=None)
        atomic_before = deepcopy(atomic_task.to_dict())

        atomic_prompt = build_atomic_prompt(atomic_task, verification_epoch="epoch")
        atomic_payload = json.loads(atomic_prompt.split("```json\n", 1)[1].split("\n```", 1)[0])

        self.assertIn("binding_dimension_catalog", atomic_payload)
        self.assertIn(BINDING_DIMENSION_INSTRUCTIONS, atomic_prompt)
        self.assertEqual(decode_review_payload(atomic_payload)["claim"], atomic_claim)
        self.assertEqual(atomic_task.to_dict(), atomic_before)
        self.assertEqual(atomic_payload["verification_input_hash"],
                         atomic_task.review_dict()["verification_input_hash"])

        context = {"shared": "exact task context" * 100}
        tasks = []
        for index in range(8):
            row = claim(f"batch.{index}", [shared])
            row.update(refs=[], fact_refs=[], dependencies=[])
            tasks.append(AtomicTask(row["claim_id"], row, {}, [], [], context=deepcopy(context)))
        batch_before = [deepcopy(task.to_dict()) for task in tasks]

        requests = pack_review_requests(tasks, batch_size=8, max_bytes=128 * 1024,
                                        verification_epoch="epoch")

        self.assertEqual(len(requests), 1)
        batch_prompt = requests[0]["prompt"]
        batch_payload = json.loads(batch_prompt.split("```json\n", 1)[1].split("\n```", 1)[0])
        self.assertIn("binding_dimension_catalog", batch_payload)
        self.assertIn("context_value_catalog", batch_payload)
        self.assertIn(BINDING_DIMENSION_INSTRUCTIONS, batch_prompt)
        self.assertIn(CONTEXT_CATALOG_INSTRUCTIONS, batch_prompt)
        self.assertEqual(decode_review_payload(batch_payload)["tasks"][0]["claim"], tasks[0].claim)
        decoded_batch = decode_review_payload(batch_payload)
        for row, task in zip(decoded_batch["tasks"], tasks):
            self.assertEqual(row["verification_input_hash"],
                             task.review_dict()["verification_input_hash"])
        self.assertEqual([task.to_dict() for task in tasks], batch_before)

    def test_scoped_review_diagnostic_survives_context_pooling(self):
        context = {"evidence_scope": {"ingestion_diagnostics": {
            "requires_scoped_review": True,
        }}, "large_shared_note": "x" * 1000}
        payload = {"tasks": [
            {"claim": claim(f"claim.{index}", []), "context": deepcopy(context)}
            for index in range(8)
        ]}
        projected = project_review_payload(payload)
        prompt = "instructions\n```json\n" + json.dumps(
            projected, ensure_ascii=False, separators=(",", ":")) + "\n```"

        self.assertIn("context_value_catalog", projected)
        self.assertTrue(requires_scoped_review(prompt))

    def test_budget_breakdown_counts_compressed_dimensions_once_and_stays_additive(self):
        shared = dimensions()
        payload = {"tasks": [
            {"claim": claim(f"claim.{index}", [shared])} for index in range(8)
        ]}
        projected = project_review_payload(payload)
        prompt = "instructions\n```json\n" + json.dumps(
            projected, ensure_ascii=False, separators=(",", ":")) + "\n```"
        schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}

        breakdown = review_wire_breakdown(prompt, schema)

        expected_own = (
            sum(json_bytes(row["claim"]) for row in projected["tasks"])
            + sum(json_bytes(value) for value in projected["binding_dimension_catalog"].values())
        )
        self.assertEqual(breakdown["own_evidence_bytes"], expected_own)
        self.assertEqual(breakdown["wire_bytes"],
                         breakdown["own_evidence_bytes"]
                         + breakdown["shared_context_bytes"]
                         + breakdown["schema_bytes"])

    def test_atomic_and_deep_defaults_are_128_kib(self):
        self.assertEqual(DEFAULT_ATOMIC_REVIEW_MAX_BYTES, 128 * 1024)
        self.assertEqual(DEFAULT_DEEP_REVIEW_MAX_BYTES, 128 * 1024)
