"""Run with python -m v4; V3 entry points remain unchanged."""
import argparse
import json
from pathlib import Path
from .runner import PipelineRunnerV4, ROLES
from .cloud import AntigravityCloudClient
from .checkpoint import save_checkpoint, load_checkpoint, write_json
from .journal import RunJournal
from .agy_session import AntigravityError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trading Agent V4 cloud-only pipeline")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--resume")
    source.add_argument("--check-transport", action="store_true",
                        help="Check tool-free CLI capabilities only; never send a user prompt")
    for role in ("author", "verifier", "deep", "global"):
        parser.add_argument(f"--{role}-model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--request-timeout", type=float, default=1200,
                        help="Per-request seconds (all roles and reviews); default 1200")
    parser.add_argument("--session-max-continuations", type=int,
                        help="Total same-conversation continuations; raise a saved ceiling without resetting usage (default 2)")
    parser.add_argument("--session-budget-seconds", type=float,
                        help="Total session time budget; raise a saved ceiling without resetting consumed time")
    parser.add_argument("--request-attempts", type=int, default=3,
                        help="Attempts per request, including same-CID continuation and JSON correction; default 3")
    parser.add_argument("--no-auto-continue", action="store_true",
                        help="Pause on a pending session instead of bounded automatic same-CID continuation")
    parser.add_argument("--max-workers", type=int, choices=range(1, 33),
                        help="Concurrent requests; default 4, resume preserves checkpoint unless overridden")
    parser.add_argument("--review-batch-size", type=int, choices=range(1, 17),
                        help="Claims per Phase 6 request; default 1 (individual); larger batches are opt-in")
    parser.add_argument("--review-batch-max-bytes", type=int,
                        help="Target UTF-8 input bytes per review batch; default 65536")
    parser.add_argument("--max-revisions", type=int,
                        help="Total revision ceiling; default 3, resume preserves checkpoint unless overridden")
    parser.add_argument("--max-contract-attempts", type=int,
                        help="Response-contract attempts per claim/input; default 3, resume can raise the saved ceiling")
    parser.add_argument("--retry-role", action="append", choices=ROLES, default=[],
                        help="Re-request an uncommitted role output instead of replaying its saved response")
    parser.add_argument("--fresh-verification", action="store_true",
                        help="Explicitly start a new verification epoch instead of continuing saved requests")
    args = parser.parse_args(argv)
    if args.review_batch_max_bytes is not None and args.review_batch_max_bytes < 1024:
        parser.error("--review-batch-max-bytes must be >=1024")
    out = Path(args.out)
    try:
        write_json(out / "result.json", dict(status="running", blocked=True))
    except Exception as exc:
        print(json.dumps(dict(status="blocked:output", blocked=True, result_written=False,
                              reason=f"{type(exc).__name__}: {exc}"), ensure_ascii=False))
        return 2
    runner, clients = None, {}
    try:
        clients = {role: AntigravityCloudClient(getattr(args, f"{role}_model"), timeout=args.request_timeout, session_root=out / "sessions",
                                                         max_continuations=args.session_max_continuations,
                                                         session_budget_seconds=args.session_budget_seconds,
                                                         request_attempts=args.request_attempts,
                                                         continue_pending=not args.no_auto_continue) for role in ("author", "verifier", "deep", "global")}
        if args.check_transport:
            capabilities = {role: client.check_capabilities() for role, client in clients.items()}
            result = dict(blocked=False, status="transport_ready", capabilities=capabilities)
        elif args.resume:
            runner = load_checkpoint(args.resume, continue_verification=not args.fresh_verification)
            if args.max_revisions is not None:
                if args.max_revisions < 0:
                    raise ValueError("max_revisions must be nonnegative")
                runner.max_revisions = max(runner.max_revisions, args.max_revisions)
            if args.max_contract_attempts is not None:
                if args.max_contract_attempts < 1:
                    raise ValueError("max_contract_attempts must be positive")
                runner.max_contract_attempts = max(runner.max_contract_attempts, args.max_contract_attempts)
            if args.max_workers is not None:
                runner.max_workers = args.max_workers
            if args.review_batch_size is not None:
                runner.review_batch_size = args.review_batch_size
            if args.review_batch_max_bytes is not None:
                runner.review_batch_max_bytes = args.review_batch_max_bytes
            runner.journal = RunJournal(out, resume_from=args.resume, retry_roles=args.retry_role,
                                        continue_verification=not args.fresh_verification)
            result = runner.resume(clients["author"], clients["verifier"], clients["deep"], clients["global"])
        else:
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            runner = PipelineRunnerV4(max_workers=args.max_workers or 4, max_revisions=args.max_revisions if args.max_revisions is not None else 3,
                                      max_contract_attempts=args.max_contract_attempts if args.max_contract_attempts is not None else 3,
                                      review_batch_size=args.review_batch_size or 1,
                                      review_batch_max_bytes=args.review_batch_max_bytes or 65536)
            runner.journal = RunJournal(out, retry_roles=args.retry_role)
            result = runner.execute(manifest["subject"]["ticker"], manifest, clients["author"], clients["verifier"], clients["deep"], clients["global"])
    except Exception as exc:
        details = exc.details() if isinstance(exc, AntigravityError) else {}
        result = dict(blocked=True, status=exc.blocked_status("blocked:input") if isinstance(exc, AntigravityError) else "blocked:input",
                      reason=f"{type(exc).__name__}: {exc}", **details)
    if args.check_transport:
        result.update(capability_check=True, user_prompt_sent=False)
    if runner is not None and runner.run_id:
        try:
            if runner.journal is not None:
                runner.journal.checkpoint(runner)
            else:
                save_checkpoint(runner, out / "checkpoint.json")
        except Exception as exc:
            result = dict(blocked=True, status="blocked:checkpoint", reason=f"{type(exc).__name__}: {exc}")
    # Only result.json carries deliverable reports. A blocked run has none;
    # old report files cannot be mistaken for this run's successful result.
    try:
        write_json(out / "result.json", {**result, "cloud_calls": {role: client.calls for role, client in clients.items()}})
    except Exception as exc:
        print(json.dumps(dict(status="blocked:output", blocked=True, result_written=False,
                              reason=f"{type(exc).__name__}: {exc}"), ensure_ascii=False))
        return 2
    print(json.dumps(dict(status=result.get("status", runner.status if runner else "blocked:input"), blocked=result["blocked"],
                          result=str(out / "result.json")), ensure_ascii=False))
    return 2 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
