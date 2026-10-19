"""Queue durability/fencing semantics; no model, runner, or LLM correctness claims."""
import json
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path

from .agent_queue import NativeWorkQueue


def state_bytes(out):
    return (Path(out) / "agent-state.json").read_bytes()


class NativeQueue(unittest.TestCase):
    def test_partial_submit_survives_reload_duplicate_is_idempotent_second_completes(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = NativeWorkQueue(temp)
            queue.create(dict(format="v4-checkpoint/1", marker="init"), "hash-a")
            packet = queue.issue("atomic_review",
                                 {"c1": dict(prompt="review one"), "c2": dict(prompt="review two")}, "hash-a")
            first = dict(claim_id="c1", verdict="PASS")
            queue.submit(packet["packet_id"], {"c1": first}, "runtime/host-1", "hash-a")
            reloaded = NativeWorkQueue(temp)
            with reloaded.transaction() as state:
                stored = state["packets"][packet["packet_id"]]["received"]["c1"]
            self.assertEqual(stored["response"], first)
            self.assertEqual(stored["executor"], "runtime/host-1")
            attempt_id = stored["attempt_id"]
            result = reloaded.submit(packet["packet_id"], {"c1": first}, "runtime/host-1", "hash-a")
            self.assertEqual(result["missing_item_ids"], ["c2"])
            with reloaded.transaction() as state:
                again = state["packets"][packet["packet_id"]]["received"]["c1"]
            self.assertEqual(again["attempt_id"], attempt_id)
            result = reloaded.submit(packet["packet_id"], {"c2": dict(claim_id="c2", verdict="FAIL")},
                                     "runtime/host-1", "hash-a")
            self.assertEqual(result["received_item_ids"], ["c1", "c2"])
            self.assertEqual(result["missing_item_ids"], [])

    def test_conflicting_duplicate_unknown_item_and_stale_input_reject_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = NativeWorkQueue(temp)
            queue.create(dict(marker="init"), "hash-a")
            packet = queue.issue("atomic_review", {"c1": dict(prompt="review one")}, "hash-a")
            queue.submit(packet["packet_id"], {"c1": dict(claim_id="c1", verdict="PASS")},
                         "runtime/host-1", "hash-a")
            untampered = state_bytes(temp)
            with self.assertRaises(ValueError):
                queue.submit(packet["packet_id"], {"c1": dict(claim_id="c1", verdict="FAIL")},
                             "runtime/host-1", "hash-a")
            with self.assertRaises(ValueError):
                queue.submit(packet["packet_id"], {"nope": dict(claim_id="nope")}, "runtime/host-1", "hash-a")
            with self.assertRaises(ValueError):
                queue.submit("0" * 32, {"c1": dict(claim_id="c1")}, "runtime/host-1", "hash-a")
            self.assertEqual(state_bytes(temp), untampered)
            with queue.transaction() as state:
                state["input_hash"] = "hash-b"
            updated = state_bytes(temp)
            with self.assertRaises(ValueError):
                queue.submit(packet["packet_id"], {"c1": dict(claim_id="c1", verdict="PASS")},
                             "runtime/host-1", "hash-a")
            self.assertEqual(state_bytes(temp), updated)
            with NativeWorkQueue(temp).transaction() as state:
                self.assertEqual(state["input_hash"], "hash-b")
                self.assertEqual(state["packets"][packet["packet_id"]]["received"]["c1"]["response"],
                                 dict(claim_id="c1", verdict="PASS"))

    def test_transaction_rollback_create_overwrite_scalar_response_and_per_item_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = NativeWorkQueue(temp)
            queue.create(dict(marker="init"), "hash-a")
            before = state_bytes(temp)
            with self.assertRaises(RuntimeError):
                with queue.transaction() as state:
                    state["packets"]["discarded"] = dict(kind="never")
                    raise RuntimeError("simulated crash before commit")
            self.assertEqual(state_bytes(temp), before)
            with self.assertRaises(ValueError):
                queue.create(dict(marker="other"), "hash-z")
            packet = queue.issue("deep_review", {"c1": dict(prompt="deep one"), "c2": dict(prompt="deep two")}, "hash-a")
            with self.assertRaises(ValueError):
                queue.submit(packet["packet_id"], {"c1": "just text"}, "runtime/host-1", "hash-a")
            queue.submit(packet["packet_id"], {"c1": dict(claim_id="c1", verdict="PASS")}, "runtime/host-1", "hash-a")
            queue.submit(packet["packet_id"], {"c2": dict(claim_id="c2", verdict="FAIL")}, "runtime/host-2", "hash-a")
            with NativeWorkQueue(temp).transaction() as state:
                received = state["packets"][packet["packet_id"]]["received"]
                self.assertEqual(received["c1"]["executor"], "runtime/host-1")
                self.assertEqual(received["c2"]["executor"], "runtime/host-2")
                self.assertNotIn("discarded", state["packets"])

    def test_concurrent_process_submissions_preserve_both_executors(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = NativeWorkQueue(temp)
            queue.create({}, "version")
            packet = queue.issue("review", {cid: dict(prompt="review") for cid in ["one", "two"]}, "version")
            script = ("import sys; from v4.agent_queue import NativeWorkQueue; "
                      "q=NativeWorkQueue(sys.argv[1]); "
                      "[q.submit(sys.argv[2],{sys.argv[3]:{'candidate':True}},sys.argv[4],'version') for _ in range(10)]")
            processes = [subprocess.Popen([sys.executable, "-B", "-c", script, temp,
                         packet["packet_id"], cid, "executor/" + cid], cwd=Path(__file__).resolve().parents[1],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for cid in ["one", "two"]]
            try:
                for process in processes:
                    stdout, stderr = process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0, stderr)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
            with queue.transaction() as state:
                received = state["packets"][packet["packet_id"]]["received"]
                self.assertEqual({cid: row["executor"] for cid, row in received.items()},
                                 {"one": "executor/one", "two": "executor/two"})
                state["packets"][packet["packet_id"]]["retired"] = True
            retired = state_bytes(temp)
            with self.assertRaisesRegex(ValueError, "retired"):
                queue.submit(packet["packet_id"], {"one": {"candidate": True}}, "executor/one", "version")
            self.assertEqual(state_bytes(temp), retired)


if __name__ == "__main__":
    unittest.main()
