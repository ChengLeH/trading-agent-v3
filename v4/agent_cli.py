"""Experimental checkpoint-exchange CLI for a resident host agent.

Portable JSON file exchange only: no provider, model, subagent, shell, or
network is ever started here. The host agent reads prompts from `next`,
answers with a JSON object mapping exact item IDs, and submits through this
CLI. This is not the complete workflow architecture; it exposes the durable
native queue for one current host agent at a time.
"""
import argparse
import json
from pathlib import Path


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


def _fail(exc):
    _emit(dict(ok=False, error=dict(type=type(exc).__name__, reason=str(exc))))
    return 2


def _result_exit(payload):
    _emit(payload)
    status = payload.get("status") if isinstance(payload, dict) else None
    blocked = bool(payload.get("blocked")) or (isinstance(status, str) and status.startswith("blocked"))
    return 1 if blocked else 0


def main(argv=None):
    from .agent_reviews import NativeReviewWorkflow

    parser = argparse.ArgumentParser(prog="python -m v4.agent_cli",
                                     description="Experimental native checkpoint exchange; no model is started.")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="import a checkpoint into a new work directory")
    origin = init.add_mutually_exclusive_group(required=True)
    origin.add_argument("--checkpoint", help="import an existing V4 checkpoint")
    origin.add_argument("--manifest", help="ingest a fresh V4 manifest without AGY")
    init.add_argument("--ticker", help="optional expected ticker; must match manifest")
    init.add_argument("--work-dir", required=True)
    init.add_argument('--role-prompt-budget-bytes', type=int, default=None,
                      help='advisory UTF-8 budget per role; never truncates source evidence')
    init.add_argument('--review-batch-max-bytes', type=int, default=None)
    init.add_argument('--review-packet-max-bytes', type=int, default=None)

    budgets = sub.add_parser('configure-reviews', help='change review delivery byte limits on an existing run')
    budgets.add_argument('--work-dir', required=True)
    budgets.add_argument('--review-batch-max-bytes', type=int, default=None)
    budgets.add_argument('--review-packet-max-bytes', type=int, default=None)
    budgets.add_argument('--reason', required=True)

    nxt = sub.add_parser("next", help="show the current outstanding packet")
    nxt.add_argument("--work-dir", required=True)
    nxt.add_argument('--prompt-files', action='store_true',
                     help='for role packets, return durable prompt paths/hashes instead of inline prompts')

    submit = sub.add_parser("submit", help="submit structured responses for a packet")
    submit.add_argument("--work-dir", required=True)
    submit.add_argument("--packet-id", required=True)
    submit.add_argument("--input-hash", required=True)
    submit.add_argument("--executor", required=True,
                        help="runtime identity declared by the answering host agent (not certified)")
    submit.add_argument("--responses", required=True,
                        help="path to a JSON object mapping exact item IDs to JSON objects/arrays")

    retry = sub.add_parser("retry-role", help="discard uncommitted candidates for one role")
    retry.add_argument("--work-dir", required=True)
    retry.add_argument("--role", required=True)

    patch = sub.add_parser("patch", help="submit Claim patches for a pending P6 repair request")
    patch.add_argument("--work-dir", required=True)
    patch.add_argument("--packet-id", required=True)
    patch.add_argument("--input-hash", required=True)
    patch.add_argument("--executor", required=True,
                       help="runtime identity declared by the answering host agent (not certified)")
    patch.add_argument("--patches", required=True,
                       help="path to a JSON array of objects, each with claim_id and changed fields")

    secondary = sub.add_parser("dispatch-secondary", help="optional AGY atomic/deep dispatch; remaining work stays with main Agent")
    secondary.add_argument("--work-dir", required=True)
    secondary.add_argument("--agy-model", required=True, help="explicit AGY secondary model ID")
    secondary.add_argument("--max-workers", type=int, default=4, choices=range(1, 9))
    secondary.add_argument("--max-items", type=int, default=16, choices=range(1, 17))

    retry_link = sub.add_parser("retry-link", help="retry a rejected finding linker mapping")
    retry_link.add_argument("--work-dir", required=True)
    retry_link.add_argument("--reason", required=True)

    retry_global = sub.add_parser("retry-global", help="explicitly retry rejected global P6 packet")
    retry_global.add_argument("--work-dir", required=True)
    retry_global.add_argument("--reason", required=True, help="operator reason; recorded durably")

    retry_patch = sub.add_parser("retry-patch", help="explicitly retry rejected Claim patch candidate")
    retry_patch.add_argument("--work-dir", required=True)
    retry_patch.add_argument("--reason", required=True, help="operator reason; recorded durably")

    restore = sub.add_parser("restore-secondary", help="explicitly re-enable a quota-disabled secondary")
    restore.add_argument("--work-dir", required=True)
    restore.add_argument("--identity", required=True)
    restore.add_argument("--reason", required=True)

    evidence = sub.add_parser("add-evidence", help="append new canonical sources without overwriting existing IDs")
    evidence.add_argument("--work-dir", required=True)
    evidence.add_argument("--manifest", required=True)
    evidence.add_argument("--request-id", help="link the imported sources to one exact evidence request")
    evidence.add_argument("--reason", required=True)

    search_result = sub.add_parser("record-evidence-search", help="record a search attempt without resolving its finding")
    search_result.add_argument("--work-dir", required=True)
    search_result.add_argument("--request-id", required=True)
    search_result.add_argument("--outcome", required=True, choices=("found", "not_found", "unavailable"))
    search_result.add_argument("--query", default="")
    search_result.add_argument("--details", default="")

    events = sub.add_parser('register-events', help='replace the explicit typed event catalog; never infer from prose')
    events.add_argument('--work-dir', required=True)
    events.add_argument('--events', required=True, help='JSON array of the complete event catalog')
    events.add_argument('--reason', required=True)

    declaration = sub.add_parser('declare-financial-source', help='explicit source extraction defaults; never change raw values')
    declaration.add_argument('--work-dir', required=True)
    declaration.add_argument('--source-id', required=True)
    declaration.add_argument('--entity')
    declaration.add_argument('--basis')
    declaration.add_argument('--reason', required=True)

    evidence_patch = sub.add_parser("repair-evidence", help="request Claim patches using new or explicitly selected existing evidence")
    evidence_patch.add_argument("--work-dir", required=True)
    evidence_patch.add_argument("--claim-id", action="append", required=True)
    evidence_patch.add_argument("--request-id", help="bind the repair to one exact evidence request")
    evidence_patch.add_argument("--source-ref", action="append",
        help="exact existing canonical source ID; repeat as needed; requires --request-id")
    evidence_patch.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            if any(v is not None and v < 1024 for v in
                   (args.review_batch_max_bytes, args.review_packet_max_bytes)):
                raise ValueError('review byte budgets must be >=1024')
            if args.role_prompt_budget_bytes is not None and args.role_prompt_budget_bytes < 1024:
                raise ValueError('role prompt budget must be >=1024')
            if args.checkpoint is not None:
                if args.ticker is not None:
                    raise ValueError("--ticker applies only to --manifest")
                source = Path(args.checkpoint)
                if not source.is_file():
                    raise ValueError(f"checkpoint not found: {source}")
                instance = NativeReviewWorkflow.from_checkpoint(source, args.work_dir)
                if args.review_batch_max_bytes is not None or args.review_packet_max_bytes is not None:
                    from .agent_reviews import configure_review_budget
                    configure_review_budget(instance, max_item_bytes=args.review_batch_max_bytes,
                        max_packet_bytes=args.review_packet_max_bytes, reason='declared at native init')
                if args.role_prompt_budget_bytes is not None:
                    with instance.queue.transaction() as state:
                        state.setdefault('options', {})['role_prompt_budget_bytes'] = args.role_prompt_budget_bytes
                _emit(dict(ok=True, status="initialized", work_dir=str(args.work_dir),
                           source_checkpoint=str(source)))
                return 0
            source = Path(args.manifest)
            if not source.is_file():
                raise ValueError(f"manifest not found: {source}")
            instance = NativeReviewWorkflow.from_manifest(source, args.work_dir, ticker=args.ticker)
            if args.review_batch_max_bytes is not None or args.review_packet_max_bytes is not None:
                from .agent_reviews import configure_review_budget
                configure_review_budget(instance, max_item_bytes=args.review_batch_max_bytes,
                    max_packet_bytes=args.review_packet_max_bytes, reason='declared at native init')
            if args.role_prompt_budget_bytes is not None:
                with instance.queue.transaction() as state:
                    state.setdefault('options', {})['role_prompt_budget_bytes'] = args.role_prompt_budget_bytes
            _emit(dict(ok=True, status="initialized", work_dir=str(args.work_dir),
                       source_manifest=str(source)))
            return 0
        if args.command == 'configure-reviews':
            from .agent_reviews import configure_review_budget
            return _result_exit(configure_review_budget(NativeReviewWorkflow(args.work_dir),
                max_item_bytes=args.review_batch_max_bytes, max_packet_bytes=args.review_packet_max_bytes,
                reason=args.reason))
        if args.command == "restore-secondary":
            from .agent_dispatch import restore_secondary_availability
            changed = restore_secondary_availability(NativeReviewWorkflow(args.work_dir), args.identity, reason=args.reason)
            return _result_exit(dict(status="secondary_restored" if changed else "secondary_not_disabled", identity=args.identity))
        if args.command == 'declare-financial-source':
            from .evidence_repair import declare_financial_source
            values = {k:v for k,v in dict(entity=args.entity,basis=args.basis).items() if v is not None}
            return _result_exit(declare_financial_source(NativeReviewWorkflow(args.work_dir), args.source_id,
                values, reason=args.reason))
        if args.command == 'register-events':
            from .evidence_repair import register_event_catalog
            return _result_exit(register_event_catalog(NativeReviewWorkflow(args.work_dir),
                json.loads(Path(args.events).read_text(encoding='utf-8')), reason=args.reason))
        if args.command == "add-evidence":
            from .evidence_repair import append_sources
            return _result_exit(append_sources(NativeReviewWorkflow(args.work_dir), args.manifest,
                reason=args.reason, request_id=args.request_id))
        if args.command == "record-evidence-search":
            from .evidence_repair import record_evidence_search
            return _result_exit(record_evidence_search(NativeReviewWorkflow(args.work_dir), args.request_id,
                args.outcome, query=args.query, details=args.details))
        if args.command == "repair-evidence":
            from .evidence_repair import prepare_evidence_patch
            return _result_exit(prepare_evidence_patch(NativeReviewWorkflow(args.work_dir), args.claim_id,
                reason=args.reason, request_id=args.request_id, source_refs=args.source_ref))
        if args.command == "next":
            payload = NativeReviewWorkflow(args.work_dir).next()
            if args.prompt_files and payload.get('kind') == 'roles':
                for item in payload['items'].values():
                    item.pop('prompt', None)
                payload['prompt_delivery'] = 'files; read each delivery.prompt_file before answering'
            return _result_exit(payload)
        if args.command == "submit":
            path = Path(args.responses)
            try:
                responses = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise ValueError(f"responses file not found: {path}") from None
            except ValueError as exc:
                raise ValueError(f"responses file is not valid JSON: {exc}") from exc
            if not isinstance(responses, dict) or not responses:
                raise ValueError("responses must be a nonempty JSON object mapping exact item IDs")
            for item_id, response in responses.items():
                if not isinstance(item_id, str) or not item_id.strip():
                    raise ValueError("response item IDs must be nonempty strings")
                if not isinstance(response, (dict, list)):
                    raise ValueError(f"response for {item_id} must be a JSON object or array")
            workflow = NativeReviewWorkflow(args.work_dir)
            return _result_exit(workflow.submit(args.packet_id, responses, args.executor, args.input_hash))
        if args.command == "patch":
            path = Path(args.patches)
            try:
                patches = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise ValueError(f"patches file not found: {path}") from None
            except ValueError as exc:
                raise ValueError(f"patches file is not valid JSON: {exc}") from exc
            if not isinstance(patches, list) or not patches:
                raise ValueError("patches must be a nonempty JSON array")
            return _result_exit(NativeReviewWorkflow(args.work_dir).submit_patch(
                args.packet_id, patches, args.executor, args.input_hash))
        if args.command == "retry-link":
            return _result_exit(NativeReviewWorkflow(args.work_dir).retry_link(args.reason))
        if args.command == "dispatch-secondary":
            # Never construct a secondary client when the next task belongs
            # to the host or when no runnable review work exists.
            from .agent_dispatch import dispatch_secondary_reviews
            workflow = NativeReviewWorkflow(args.work_dir)
            pending = workflow.next()
            if pending.get("kind") not in ("atomic", "deep"):
                return _result_exit(dict(status="host_owned", packet=pending,
                                         reason="current work belongs to the host Agent"))
            try:
                from .cloud import AntigravityCloudClient
                client = AntigravityCloudClient(args.agy_model)
            except (RuntimeError, ValueError) as exc:
                return _result_exit(dict(status="fallback_to_main",
                                         reason=f"{type(exc).__name__}: {exc}",
                                         warnings=[workflow.record_secondary_event(pending, code='secondary_unavailable',
                                             requested_identity='antigravity/' + args.agy_model,
                                             failed_claim_ids=pending['items'])],
                                         packet=pending))
            return _result_exit(dispatch_secondary_reviews(
                workflow, client, max_workers=args.max_workers,
                max_items=args.max_items))
        if args.command == "retry-global":
            return _result_exit(NativeReviewWorkflow(args.work_dir).retry_global(args.reason))
        if args.command == "retry-patch":
            return _result_exit(NativeReviewWorkflow(args.work_dir).retry_patch(args.reason))
        return _result_exit(NativeReviewWorkflow(args.work_dir).retry_role(args.role))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return _fail(exc)
    except Exception as exc:  # surface unexpected failures as strict JSON too
        return _fail(exc)


if __name__ == "__main__":
    raise SystemExit(main())
