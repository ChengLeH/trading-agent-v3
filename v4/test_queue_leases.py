"""Durable per-item queue reservations and submission fencing."""
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from .agent_queue import NativeWorkQueue, claim_items_in_state, submit_items_in_state


def state_bytes(directory):
    return (Path(directory) / "agent-state.json").read_bytes()


class NativeQueueLeases(unittest.TestCase):
    def make_queue(self, directory, item_ids=("a", "b", "c")):
        queue = NativeWorkQueue(directory)
        queue.create(dict(marker="init"), "hash-a")
        packet = queue.issue("review", {item_id: dict(prompt=f"review {item_id}")
                                         for item_id in item_ids}, "hash-a")
        return queue, packet

    def test_claims_are_durable_grouped_and_disjoint(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet = self.make_queue(directory)
            with patch("v4.agent_queue.time.time", return_value=100.0):
                first = queue.claim_items(packet["packet_id"], "host/one", ["a", "b"], 30)
            self.assertEqual(first["packet_id"], packet["packet_id"])
            self.assertEqual(first["owner"], "host/one")
            self.assertEqual(first["item_ids"], ["a", "b"])
            self.assertEqual(first["expires_at"], 130.0)
            self.assertTrue(first["lease_id"])

            reloaded = NativeWorkQueue(directory)
            with reloaded.transaction() as state:
                rows = state["packets"][packet["packet_id"]]["item_leases"]
                self.assertEqual(rows["a"], dict(lease_id=first["lease_id"], owner="host/one",
                                                  expires_at=130.0))
                self.assertEqual(rows["b"], rows["a"])

            before = state_bytes(directory)
            with patch("v4.agent_queue.time.time", return_value=101.0):
                with self.assertRaisesRegex(ValueError, "leased"):
                    reloaded.claim_items(packet["packet_id"], "host/two", ["b", "c"], 30)
                self.assertEqual(state_bytes(directory), before)
                second = reloaded.claim_items(packet["packet_id"], "host/two", ["c"], 30)
            self.assertNotEqual(second["lease_id"], first["lease_id"])
            self.assertEqual(second["item_ids"], ["c"])
            self.assertNotEqual(before, state_bytes(directory))

    def test_concurrent_claims_of_one_item_have_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet = self.make_queue(directory, ("a",))
            marker = Path(directory) / "start-claims"
            script = """import sys, time
from pathlib import Path
from v4.agent_queue import NativeWorkQueue
directory, packet_id, marker, executor = sys.argv[1:]
while not Path(marker).exists():
    time.sleep(0.001)
try:
    NativeWorkQueue(directory).claim_items(packet_id, executor, ["a"], 30)
except ValueError:
    raise SystemExit(7)
"""
            processes = [subprocess.Popen(
                [sys.executable, "-B", "-c", script, directory, packet["packet_id"],
                 str(marker), f"host/{index}"], cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for index in (1, 2)]
            try:
                marker.touch()
                results = [process.communicate(timeout=15) for process in processes]
                self.assertCountEqual([process.returncode for process in processes], [0, 7],
                                      [stderr for _, stderr in results])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.communicate()
            with NativeWorkQueue(directory).transaction() as state:
                leases = state["packets"][packet["packet_id"]]["item_leases"]
                self.assertEqual(list(leases), ["a"])

    def test_default_claim_skips_received_and_live_rows_then_expired_claim_can_reclaim(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet = self.make_queue(directory, ("done", "held", "free"))
            queue.submit(packet["packet_id"], {"done": {"candidate": True}}, "legacy/host", "hash-a")

            with patch("v4.agent_queue.time.time", return_value=100.0):
                held = queue.claim_items(packet["packet_id"], "host/one", ["held"], 10)
            with patch("v4.agent_queue.time.time", return_value=105.0):
                free = queue.claim_items(packet["packet_id"], "host/two", lease_seconds=10)
            self.assertEqual(free["item_ids"], ["free"])

            with patch("v4.agent_queue.time.time", return_value=111.0):
                with self.assertRaisesRegex(ValueError, "expired"):
                    queue.submit(packet["packet_id"], {"held": {"candidate": "late"}},
                                 "host/one", "hash-a")
                reclaimed = queue.claim_items(packet["packet_id"], "host/two", ["held"], 10)
            self.assertNotEqual(reclaimed["lease_id"], held["lease_id"])
            with patch("v4.agent_queue.time.time", return_value=112.0):
                with self.assertRaisesRegex(ValueError, "another executor"):
                    queue.submit(packet["packet_id"], {"held": {"candidate": "late"}},
                                 "host/one", "hash-a")
                queue.submit(packet["packet_id"], {"held": {"candidate": "current"}},
                             "host/two", "hash-a")
                queue.submit(packet["packet_id"], {"free": {"candidate": "free"}},
                             "host/two", "hash-a")
            with NativeWorkQueue(directory).transaction() as state:
                packet_state = state["packets"][packet["packet_id"]]
                self.assertNotIn("held", packet_state.get("item_leases", {}))
                self.assertNotIn("free", packet_state.get("item_leases", {}))
                self.assertEqual(packet_state["received"]["held"]["executor"], "host/two")

    def test_renew_and_release_are_bound_to_owner_and_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet = self.make_queue(directory)
            with patch("v4.agent_queue.time.time", return_value=100.0):
                claim = queue.claim_items(packet["packet_id"], "host/one", ["a", "b"], 10)
            with patch("v4.agent_queue.time.time", return_value=105.0):
                renewed = queue.renew_items(packet["packet_id"], "host/one", claim["lease_id"], 20)
            self.assertEqual(renewed["expires_at"], 125.0)
            self.assertEqual(renewed["item_ids"], ["a", "b"])

            before = state_bytes(directory)
            with patch("v4.agent_queue.time.time", return_value=106.0):
                with self.assertRaisesRegex(ValueError, "owner"):
                    queue.renew_items(packet["packet_id"], "host/two", claim["lease_id"], 20)
                with self.assertRaisesRegex(ValueError, "owner"):
                    queue.release_items(packet["packet_id"], "host/two", claim["lease_id"])
            self.assertEqual(state_bytes(directory), before)

            with patch("v4.agent_queue.time.time", return_value=126.0):
                expired_renewal = queue.renew_items(packet["packet_id"], "host/one",
                                                    claim["lease_id"], 20)
                released = queue.release_items(packet["packet_id"], "host/one", claim["lease_id"])
                released_again = queue.release_items(packet["packet_id"], "host/one", claim["lease_id"])
                renewed_after_release = queue.renew_items(packet["packet_id"], "host/one",
                                                          claim["lease_id"], 20)
                reclaimed = queue.claim_items(packet["packet_id"], "host/two", ["a", "b"], 20)
            self.assertEqual(expired_renewal["item_ids"], [])
            self.assertEqual(released["item_ids"], ["a", "b"])
            self.assertEqual(released_again["item_ids"], [])
            self.assertEqual(renewed_after_release["item_ids"], [])
            self.assertNotEqual(reclaimed["lease_id"], claim["lease_id"])

    def test_exact_claim_ids_reject_unknown_duplicate_received_retired_and_stale_items(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet = self.make_queue(directory, ("received", "available"))
            queue.submit(packet["packet_id"], {"received": {"candidate": True}}, "legacy/host", "hash-a")
            for item_ids, message in ((["missing"], "unknown item"),
                                      (["available", "available"], "duplicate item"),
                                      (["received"], "already been received")):
                with self.subTest(item_ids=item_ids):
                    before = state_bytes(directory)
                    with self.assertRaisesRegex(ValueError, message):
                        queue.claim_items(packet["packet_id"], "host/one", item_ids)
                    self.assertEqual(state_bytes(directory), before)

            with queue.transaction() as state:
                state["packets"][packet["packet_id"]]["retired"] = True
            before = state_bytes(directory)
            with self.assertRaisesRegex(ValueError, "retired"):
                queue.claim_items(packet["packet_id"], "host/one", ["available"])
            self.assertEqual(state_bytes(directory), before)
            with patch("v4.agent_queue.time.time", return_value=100.0):
                renewed = queue.renew_items(packet["packet_id"], "host/one", "already-gone")
                released = queue.release_items(packet["packet_id"], "host/one", "already-gone")
            self.assertEqual(renewed["item_ids"], [])
            self.assertEqual(released["item_ids"], [])

            with queue.transaction() as state:
                state["packets"][packet["packet_id"]].pop("retired")
                state["input_hash"] = "hash-b"
            before = state_bytes(directory)
            with self.assertRaisesRegex(ValueError, "stale packet"):
                queue.claim_items(packet["packet_id"], "host/one", ["available"])
            self.assertEqual(state_bytes(directory), before)

    def test_ttl_must_be_finite_and_positive(self):
        invalid_ttls = (0, -1, float("inf"), float("nan"), True, None, "10")
        with tempfile.TemporaryDirectory() as directory:
            queue, packet = self.make_queue(directory)
            before = state_bytes(directory)
            with patch("v4.agent_queue.time.time", return_value=100.0):
                for ttl in invalid_ttls:
                    with self.subTest(ttl=ttl):
                        with self.assertRaisesRegex(ValueError, "lease_seconds"):
                            queue.claim_items(packet["packet_id"], "host/one", ["a"], ttl)
            self.assertEqual(state_bytes(directory), before)

            with patch("v4.agent_queue.time.time", return_value=100.0):
                claim = queue.claim_items(packet["packet_id"], "host/one", ["a"], 10)
            before = state_bytes(directory)
            with patch("v4.agent_queue.time.time", return_value=101.0):
                for ttl in invalid_ttls:
                    with self.subTest(renew_ttl=ttl):
                        with self.assertRaisesRegex(ValueError, "lease_seconds"):
                            queue.renew_items(packet["packet_id"], "host/one", claim["lease_id"], ttl)
            self.assertEqual(state_bytes(directory), before)

    def test_state_helper_claims_without_opening_nested_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            queue, packet = self.make_queue(directory, ("a", "b"))
            with patch("v4.agent_queue.time.time", return_value=200.0):
                with queue.transaction() as state:
                    claim = claim_items_in_state(state, packet["packet_id"], "host/one", ["a", "b"], 5)
            with patch("v4.agent_queue.time.time", return_value=201.0):
                with queue.transaction() as state:
                    submitted = submit_items_in_state(
                        state, packet["packet_id"], {"a": {"candidate": "a"}}, "host/one", "hash-a")
            self.assertEqual(claim["item_ids"], ["a", "b"])
            self.assertEqual(submitted["received_item_ids"], ["a"])
            with NativeWorkQueue(directory).transaction() as state:
                packet_state = state["packets"][packet["packet_id"]]
                self.assertEqual(set(packet_state["item_leases"]), {"b"})
                self.assertEqual(packet_state["received"]["a"]["executor"], "host/one")


if __name__ == "__main__":
    unittest.main()
