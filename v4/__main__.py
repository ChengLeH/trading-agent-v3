"""Run with python -m v4; V3 entry points remain unchanged."""
import argparse
import json
from pathlib import Path
from .runner import PipelineRunnerV4
from .cloud import GeminiCloudClient
from .checkpoint import save_checkpoint, load_checkpoint, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trading Agent V4 cloud-only pipeline")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--resume")
    for role in ("author", "verifier", "deep", "global"):
        parser.add_argument(f"--{role}-model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--max-revisions", type=int, default=3)
    args = parser.parse_args(argv)
    clients = {role: GeminiCloudClient(getattr(args, f"{role}_model")) for role in ("author", "verifier", "deep", "global")}
    if clients["verifier"].identity == clients["deep"].identity:
        parser.error("--verifier-model and --deep-model must differ")
    if args.resume:
        runner = load_checkpoint(args.resume)
        result = runner.execute_phase6(clients["verifier"], clients["deep"], clients["author"], clients["global"])
    else:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        runner = PipelineRunnerV4(max_workers=args.max_workers, max_revisions=args.max_revisions)
        result = runner.execute(manifest["subject"]["ticker"], manifest, clients["author"], clients["verifier"], clients["deep"], clients["global"])
    out = Path(args.out)
    write_json(out / "result.json", {**result, "cloud_calls": {role: client.calls for role, client in clients.items()}})
    if runner.run_id:
        save_checkpoint(runner, out / "checkpoint.json")
    # Only result.json carries deliverable reports. A blocked run has none;
    # old report files cannot be mistaken for this run's successful result.
    print(json.dumps(dict(status=result.get("status", runner.status), blocked=result["blocked"],
                          result=str(out / "result.json")), ensure_ascii=False))
    return 2 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
