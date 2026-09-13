from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from promiseledger.adapters.fixture import FixtureAdapter
from promiseledger.agent import PromiseLedgerAgent
from promiseledger.grader import GradeReport, grade_worlds
from promiseledger.seed import load_scenario, scenarios
from promiseledger.trace import TraceWriter


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_fixture(
    world: dict[str, Any], output: Path, run_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    output.mkdir(parents=True, exist_ok=True)
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    trace = TraceWriter(output / "trace.jsonl", run_id)
    adapter = FixtureAdapter(world, trace=trace.emit)
    report = PromiseLedgerAgent(adapter, run_id, trace=trace.emit).run()
    final = adapter.snapshot()
    _write_json(output / "report.json", report.as_dict())
    _write_json(output / "final.json", final)
    return final, report.as_dict()


def command_seed(args: argparse.Namespace) -> int:
    _write_json(args.output, load_scenario(args.scenario))
    print(f"seeded {args.scenario} -> {args.output}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    initial = _read_json(args.fixture)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    _write_json(output / "initial.json", initial)
    _, report = _run_fixture(initial, output, args.run_id)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["refused"] or report["verified"] else 2


def command_grade(args: argparse.Namespace) -> int:
    replay = _read_json(args.replay) if args.replay else None
    report = grade_worlds(_read_json(args.initial), _read_json(args.final), replay)
    print(report.markdown(), end="")
    return 0 if report.passed else 1


def command_eval(args: argparse.Namespace) -> int:
    root = Path(args.output)
    rows: list[tuple[str, str, str]] = []
    all_expected = True
    for name, initial in scenarios().items():
        scenario_dir = root / name
        scenario_dir.mkdir(parents=True, exist_ok=True)
        _write_json(scenario_dir / "initial.json", initial)
        final, _agent_report = _run_fixture(initial, scenario_dir, f"eval-{name}")
        replay = None
        if initial["scenario"].get("replay"):
            first_final = copy.deepcopy(final)
            replay_dir = scenario_dir / "replay"
            replay, _ = _run_fixture(first_final, replay_dir, f"eval-{name}-replay")
            _write_json(scenario_dir / "after-first.json", first_final)
            _write_json(scenario_dir / "final.json", replay)
            report = grade_worlds(initial, first_final, replay)
        else:
            report = grade_worlds(initial, final)
        (scenario_dir / "grade.md").write_text(report.markdown(), encoding="utf-8")
        _write_json(scenario_dir / "grade.json", report.as_dict())
        expected = initial["scenario"]["expected_grader"]
        observed = "pass" if report.passed else "fail"
        matched = observed == expected or (expected == "refuse" and report.passed)
        all_expected = all_expected and matched
        rows.append((name, expected.upper(), observed.upper()))

    print("| Scenario | Expected | Observed |")
    print("|---|---|---|")
    for name, expected, observed in rows:
        print(f"| {name} | {expected} | {observed} |")
    print(f"\nEvaluation contract: {'PASS' if all_expected else 'FAIL'}")
    return 0 if all_expected else 1


def command_waive(args: argparse.Namespace) -> int:
    adapter = FixtureAdapter.from_path(args.fixture)
    adapter.record_human_waiver(args.ticket_id, args.draft_id, args.approver)
    adapter.save(args.output)
    print(f"recorded human waiver in {args.output}; draft remains unsent")
    return 0


def command_run_live(args: argparse.Namespace) -> int:
    from promiseledger.adapters.live import LiveAdapter

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    trace = TraceWriter(output / "trace.jsonl", f"live-{uuid.uuid4().hex[:12]}")
    adapter = LiveAdapter.from_environment(trace=trace.emit)
    initial = adapter.snapshot()
    _write_json(output / "initial.json", initial)
    report = PromiseLedgerAgent(adapter, trace.run_id, trace=trace.emit).run()
    final = adapter.snapshot()
    _write_json(output / "final.json", final)
    _write_json(output / "report.json", report.as_dict())
    print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    return 0 if report.verified else 2


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="promiseledger")
    commands = root.add_subparsers(dest="command", required=True)

    seed = commands.add_parser("seed", help="write an isolated fixture world")
    seed.add_argument("--scenario", choices=sorted(scenarios()), required=True)
    seed.add_argument("--output", required=True)
    seed.set_defaults(handler=command_seed)

    run = commands.add_parser("run", help="run the agent against a fixture")
    run.add_argument("--fixture", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--run-id")
    run.set_defaults(handler=command_run)

    grade = commands.add_parser("grade", help="grade final state independently")
    grade.add_argument("--initial", required=True)
    grade.add_argument("--final", required=True)
    grade.add_argument("--replay")
    grade.set_defaults(handler=command_grade)

    evaluate = commands.add_parser("eval", help="run all reliability scenarios")
    evaluate.add_argument("--output", required=True)
    evaluate.set_defaults(handler=command_eval)

    waive = commands.add_parser("waive", help="record an explicit human waiver")
    waive.add_argument("--fixture", required=True)
    waive.add_argument("--ticket-id", required=True)
    waive.add_argument("--draft-id", required=True)
    waive.add_argument("--approver", required=True)
    waive.add_argument("--output", required=True)
    waive.set_defaults(handler=command_waive)

    live = commands.add_parser("run-live", help="run against disposable live workspaces")
    live.add_argument("--output", required=True)
    live.set_defaults(handler=command_run_live)
    return root


def main() -> None:
    args = parser().parse_args()
    try:
        code = args.handler(args)
    except (KeyError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
