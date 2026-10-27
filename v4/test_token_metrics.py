"""Read-only accounting for native work queues."""
import json
from pathlib import Path
import tempfile
import unittest

from .agent_queue import LEGACY_STATE_FORMAT, NativeWorkQueue
from .task_delivery import create_delivery
from .token_metrics import audit_native_work


class TokenMetricsTests(unittest.TestCase):
    def test_legacy_inline_state_is_counted_without_creating_or_rewriting_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shared = "shared prefix 漢字\n\n```json\n"
            raw = json.dumps({
                "format": LEGACY_STATE_FORMAT,
                "input_hash": "input-a",
                "packets": {
                    "p1": {"kind": "atomic", "items": {
                        "a": {"prompt": shared + "one"},
                        "b": {"prompt": shared + "two"},
                    }, "received": {}},
                    "p2": {"kind": "deep", "items": {
                        "c": {"prompt": "plain"},
                    }, "received": {}},
                },
            }, ensure_ascii=False).encode("utf-8")
            (root / "agent-state.json").write_bytes(raw)

            metrics = audit_native_work(root)

            self.assertEqual(metrics["state_format"], "v4-native-work/1")
            self.assertEqual(metrics["by_kind"]["atomic"], {
                "packet_count": 1, "item_count": 2, "prompt_count": 2,
                "prompt_bytes": len((shared + "one").encode()) + len((shared + "two").encode()),
            })
            self.assertEqual(metrics["counts"], {"packet_count": 2, "item_count": 3, "prompt_count": 3})
            self.assertEqual(metrics["physical_bytes"]["state_file_bytes"], len(raw))
            self.assertEqual(metrics["physical_bytes"]["native_archive_bytes"], 0)
            self.assertEqual(metrics["duplicate_prompt_prefixes"]["repeated_prefix_groups"], 1)
            self.assertEqual(metrics["duplicate_prompt_prefixes"]["deduplicable_prefix_bytes"], len(shared.encode()))
            self.assertEqual(metrics["usage_tokens"]["input_tokens"], None)
            self.assertEqual((root / "agent-state.json").read_bytes(), raw)
            self.assertFalse((root / "agent-state.lock").exists())

    def test_cold_archived_v2_state_is_hydrated_read_only_and_counts_physical_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = NativeWorkQueue(temp)
            queue.create({}, "input-a")
            shared = "shared prefix\n\n```json\n"
            retired = queue.issue("atomic", {
                "a": {"prompt": shared + "one"},
                "b": {"prompt": shared + "two"},
            }, "input-a")
            with queue.transaction() as state:
                state["packets"][retired["packet_id"]]["retired"] = True
            queue.issue("deep", {"c": {"prompt": "active prompt"}}, "input-a")

            state_before = queue.state_path.read_bytes()
            state_mtime = queue.state_path.stat().st_mtime_ns
            archives = sorted((Path(temp) / "native-archive").iterdir())
            archive_before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in archives}

            metrics = audit_native_work(temp)

            self.assertEqual(metrics["state_format"], "v4-native-work/2")
            self.assertEqual(metrics["counts"], {"packet_count": 2, "item_count": 3, "prompt_count": 3})
            self.assertEqual(metrics["by_kind"]["atomic"]["prompt_count"], 2)
            self.assertEqual(metrics["by_kind"]["deep"]["prompt_bytes"], len(b"active prompt"))
            self.assertEqual(metrics["physical_bytes"]["state_file_bytes"], len(state_before))
            self.assertEqual(metrics["physical_bytes"]["native_archive_bytes"], sum(len(row[0]) for row in archive_before.values()))
            self.assertEqual(metrics["duplicate_prompt_prefixes"]["deduplicable_prefix_bytes"], len(shared.encode()))
            self.assertEqual(queue.state_path.read_bytes(), state_before)
            self.assertEqual(queue.state_path.stat().st_mtime_ns, state_mtime)
            self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                              for p in (Path(temp) / "native-archive").iterdir()}, archive_before)

    def test_corrupt_cold_archive_fails_closed_without_rewriting_state(self):
        with tempfile.TemporaryDirectory() as temp:
            queue = NativeWorkQueue(temp)
            queue.create({}, "input-a")
            packet = queue.issue("atomic", {"a": {"prompt": "long enough " * 100}}, "input-a")
            with queue.transaction() as state:
                state["packets"][packet["packet_id"]]["retired"] = True
            state_before = queue.state_path.read_bytes()
            object_path = next((Path(temp) / "native-archive").glob("*.json"))
            object_path.chmod(0o644)
            object_path.write_text("{}", encoding="utf-8")
            corrupt_before = object_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "native archive|hash"):
                audit_native_work(temp)

            self.assertEqual(queue.state_path.read_bytes(), state_before)
            self.assertEqual(object_path.read_bytes(), corrupt_before)

    def test_prepared_wire_requests_are_counted_separately_from_canonical_item_prompts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = {
                "format": LEGACY_STATE_FORMAT,
                "input_hash": "input-a",
                "packets": {
                    "p-atomic": {"kind": "atomic", "items": {
                        "claim.a": {"prompt": "canonical item prompt"},
                        "claim.b": {"prompt": "another canonical prompt"},
                    }, "received": {}, "wire_requests": [
                        {"request_id": "r1", "prompt": "prepared batched wire one"},
                        {"request_id": "r2", "prompt": "prepared batched wire two"},
                    ]},
                    "p-deep": {"kind": "deep", "items": {
                        "claim.c": {"prompt": "canonical deep prompt"},
                    }, "received": {}, "wire_requests": {
                        "r3": {"request_id": "r3", "prompt": "prepared deep wire"},
                    }},
                },
            }
            (root / "agent-state.json").write_text(json.dumps(state), encoding="utf-8")

            metrics = audit_native_work(root)

            self.assertEqual(metrics["prompt_bytes"], len(b"canonical item prompt")
                             + len(b"another canonical prompt") + len(b"canonical deep prompt"))
            self.assertEqual(metrics["by_kind"]["atomic"]["prompt_bytes"],
                             len(b"canonical item prompt") + len(b"another canonical prompt"))
            self.assertEqual(metrics["wire_requests_by_kind"]["atomic"], {
                "request_count": 2, "prompt_count": 2,
                "prompt_bytes": len(b"prepared batched wire one") + len(b"prepared batched wire two"),
            })
            self.assertEqual(metrics["wire_requests_by_kind"]["deep"], {
                "request_count": 1, "prompt_count": 1,
                "prompt_bytes": len(b"prepared deep wire"),
            })
            self.assertEqual(metrics["wire_request_prompt_bytes"],
                             len(b"prepared batched wire one")
                             + len(b"prepared batched wire two") + len(b"prepared deep wire"))

    def test_task_delivery_object_store_bytes_and_file_count_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "agent-state.json").write_text(json.dumps({
                "format": LEGACY_STATE_FORMAT, "input_hash": "x", "packets": {},
            }), encoding="utf-8")
            create_delivery(root, {"items": {
                "a": {"prompt": "original prompt A"},
                "b": {"prompt": "original prompt B"},
            }})
            objects = sorted((root / "task-delivery" / "objects").iterdir())

            metrics = audit_native_work(root)

            self.assertEqual(metrics["physical_bytes"]["task_delivery_object_bytes"],
                             sum(path.stat().st_size for path in objects))
            self.assertEqual(metrics["physical_bytes"]["task_delivery_object_file_count"], len(objects))
            self.assertEqual(metrics["physical_bytes"]["state_plus_archives_and_task_delivery_file_bytes"],
                             metrics["physical_bytes"]["state_file_bytes"]
                             + metrics["physical_bytes"]["archive_bytes"]
                             + metrics["physical_bytes"]["task_delivery_object_bytes"])


if __name__ == "__main__":
    unittest.main()
