"""Command-line interface for the community-noise evidence application.

Typical session, all against the same local project:

    python -m noise_project evidence --asof 21
    python -m noise_project evidence --adopt "9 月协商会口径"
    python -m noise_project correct M3 13 51 --learned-day 21
    python -m noise_project plan
    python -m noise_project sign A1 --day 19
    python -m noise_project cancel A1 --day 25
    python -m noise_project confirm-cancel A1 --day 26
    python -m noise_project accept A3 --building L2 --units 0.5 --day 24
    python -m noise_project budget Z2 --delta -500 --day 26
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analysis import Analyzer, Params, Planner
from .errors import SourceError, StoreError, WorkflowError
from .render import render_evidence, render_plan
from .sources import load_dataset
from .store import DEFAULT_STATE_PATH, Store


def _default_sources() -> Path:
    return Path(__file__).resolve().parents[1] / "sources"


def _build_analyzer(dataset, store: Store, asof: int | None, params: Params):
    overlay = store.corrections()
    return Analyzer(dataset, params=params, overlay=overlay), asof


def _effective_asof(store: Store, asof: int | None) -> int | None:
    if asof is not None:
        return asof
    return None


def _plan(dataset, store: Store, report, baseline_report=None):
    return Planner(
        dataset,
        report,
        commitments=store.effective_commitments(dataset),
        acceptances=store.effective_acceptances(dataset),
        budgets=store.effective_budgets(dataset),
        baseline_report=baseline_report,
    ).plan()


def _params_from_args(args) -> Params:
    return Params(
        corroboration_min_sensors=args.corroboration,
        eligibility_min_nights=args.eligibility,
        background_tolerance_db=args.background_tolerance,
        control_tolerance_db=args.control_tolerance,
    )


def cmd_evidence(args) -> int:
    dataset = load_dataset(args.sources)
    store = Store(args.state)
    params = _params_from_args(args)
    analyzer = Analyzer(dataset, params=params, overlay=store.corrections())
    report = analyzer.report(asof=args.asof)
    if args.json:
        print(report.to_json())
    else:
        print(render_evidence(report))
    if args.adopt:
        snapshot = store.add_snapshot(args.adopt, report.as_dict())
        store.save()
        print(
            f"\n已采用口径快照 {snapshot['id']}（{args.adopt}），"
            f"摘要 {report.digest[:12]}，可回查。",
            file=sys.stderr,
        )
    return 0


def cmd_plan(args) -> int:
    dataset = load_dataset(args.sources)
    store = Store(args.state)
    params = _params_from_args(args)
    analyzer = Analyzer(dataset, params=params, overlay=store.corrections())
    report = analyzer.report(asof=args.asof)
    baseline = None
    if args.since_snapshot:
        snapshot = store.snapshot(args.since_snapshot)
        baseline = _report_from_snapshot(dataset, store, snapshot, params)
    elif args.asof is not None and not args.no_baseline:
        # Compare with the most recent adopted snapshot before this as-of day.
        previous = [
            snap
            for snap in store.snapshots()
            if snap["asof_day"] <= args.asof
        ]
        if previous:
            baseline = _report_from_snapshot(dataset, store, previous[-1], params)
    plan = _plan(dataset, store, report, baseline)
    if args.json:
        print(plan.to_json())
    else:
        print(render_plan(plan))
    return 0


def _report_from_snapshot(dataset, store: Store, snapshot: dict, params: Params):
    """Rebuild an adopted report at its recorded as-of day.

    Only corrections learned by that day are applied, so the rebuilt baseline
    matches the numbers the meeting actually adopted.
    """

    analyzer = Analyzer(dataset, params=params, overlay=store.corrections())
    return analyzer.report(asof=snapshot["asof_day"])


def cmd_correct(args) -> int:
    dataset = load_dataset(args.sources)
    store = Store(args.state)
    if (args.sensor, args.night) not in dataset.readings and not any(
        (row["sensor"], row["night"]) == (args.sensor, args.night)
        for row in store.corrections()
    ):
        raise SourceError(f"no observation for {args.sensor}/night {args.night}")
    row = store.add_correction(
        sensor=args.sensor,
        night=args.night,
        corrected_db=args.corrected_db,
        learned_day=args.learned_day,
        reason=args.reason,
        revision=args.revision,
    )
    store.save()
    print(f"已登记校正 {row['revision']}: {args.sensor}/夜 {args.night} -> {args.corrected_db} dB")
    print(f"该校正到第 {args.learned_day} 天才生效。")
    return 0


def cmd_sign(args) -> int:
    dataset = load_dataset(args.sources)
    store = Store(args.state)
    offer = dataset.offers.get(args.remedy)
    if offer is None:
        raise SourceError(f"unknown remedy {args.remedy}")
    row = store.add_commitment(
        remedy=args.remedy,
        day=args.day,
        operator=offer.operator,
        cents=offer.cents,
        units=offer.units,
        note=args.note,
    )
    store.save()
    print(f"已登记签署 {row['remedy']}（{row['operator']}），{row['cents']} 分，第 {row['day']} 天。")
    print("承诺在取消被确认前一直保留资金。")
    return 0


def cmd_cancel(args) -> int:
    dataset = load_dataset(args.sources)
    store = Store(args.state)
    row = store.request_cancel(args.remedy, args.day, args.reason)
    store.save()
    print(f"{args.remedy} 已申请取消（第 {args.day} 天）。取消尚未确认，资金仍保留。")
    _ = row
    return 0


def cmd_confirm_cancel(args) -> int:
    dataset = load_dataset(args.sources)
    store = Store(args.state)
    row = store.confirm_cancel(args.remedy, args.day)
    store.save()
    print(f"{args.remedy} 取消已确认（第 {args.day} 天），其保留资金现可重新安排。")
    _ = row
    return 0


def cmd_accept(args) -> int:
    dataset = load_dataset(args.sources)
    store = Store(args.state)
    # Source-recorded acceptances are part of history; the ledger must respect
    # them so the same building/night is not claimed twice.
    prior = [
        {
            "remedy": event.remedy,
            "building": event.actual_building,
            "units": float(event.actual_units or 0),
            "nights": [],
            "day": event.day,
        }
        for event in dataset.events
        if event.state == "partial_acceptance"
    ]
    row = store.add_acceptance(
        remedy=args.remedy,
        building=args.building,
        units=args.units,
        day=args.day,
        nights=args.night or [],
        note=args.note,
        prior=prior,
    )
    store.save()
    print(
        f"已登记验收 {row['remedy']}"
        + (f" / {row['building']}" if row["building"] else "")
        + f"：{row['units']:g} 单位，第 {row['day']} 天。"
    )
    print("验收只记入所指楼栋与份额，不把整片区域记为已改善。")
    return 0


def cmd_budget(args) -> int:
    dataset = load_dataset(args.sources)
    store = Store(args.state)
    if args.area not in dataset.budgets:
        raise SourceError(f"unknown budget area {args.area}")
    row = store.add_budget_adjustment(args.area, args.delta, args.day, args.reason)
    store.save()
    print(f"已登记 {args.area} 预算调整 {row['delta_cents']:+d} 分（第 {row['day']} 天）。")
    return 0


def cmd_snapshots(args) -> int:
    store = Store(args.state)
    snapshots = store.snapshots()
    if args.json:
        print(json.dumps(snapshots, ensure_ascii=False, indent=2))
        return 0
    if not snapshots:
        print("尚无已采用口径。")
        return 0
    for snapshot in snapshots:
        print(
            f"{snapshot['id']}  {snapshot['label']}  "
            f"as-of {snapshot['asof_day']}  摘要 {snapshot['digest'][:12]}  "
            f"采用于 {snapshot['adopted_at']}"
        )
    return 0


def cmd_show(args) -> int:
    store = Store(args.state)
    snapshot = store.snapshot(args.snapshot_id)
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print(f"快照 {snapshot['id']}（{snapshot['label']}）")
        print(f"as-of 第 {snapshot['asof_day']} 天；摘要 {snapshot['digest']}")
        print(json.dumps(snapshot["report"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_audit(args) -> int:
    store = Store(args.state)
    for entry in store.audit_log():
        print(f"{entry['id']}  {entry['at']}  {entry['action']}  {json.dumps(entry['detail'], ensure_ascii=False)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="noise_project",
        description="社区夜间噪声证据与分时整改承诺（本地应用）",
    )
    parser.add_argument("--sources", default=str(_default_sources()), help="source CSV directory")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="ledger path")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--asof", type=int, default=None, help="as-of day (default: latest learned correction)")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    evidence = sub.add_parser("evidence", parents=[common], help="recompute the evidence report")
    evidence.add_argument("--adopt", metavar="LABEL", help="adopt this report as a snapshot")
    evidence.add_argument("--corroboration", type=int, default=2)
    evidence.add_argument("--eligibility", type=int, default=3)
    evidence.add_argument("--background-tolerance", type=float, default=1.0)
    evidence.add_argument("--control-tolerance", type=float, default=2.0)
    evidence.set_defaults(func=cmd_evidence)

    plan = sub.add_parser("plan", parents=[common], help="submit a remediation budget plan")
    plan.add_argument("--since-snapshot", help="compare against this adopted snapshot")
    plan.add_argument("--no-baseline", action="store_true", help="do not compare with a snapshot")
    plan.add_argument("--corroboration", type=int, default=2)
    plan.add_argument("--eligibility", type=int, default=3)
    plan.add_argument("--background-tolerance", type=float, default=1.0)
    plan.add_argument("--control-tolerance", type=float, default=2.0)
    plan.set_defaults(func=cmd_plan)

    correct = sub.add_parser("correct", help="register a measurement correction")
    correct.add_argument("sensor")
    correct.add_argument("night", type=int)
    correct.add_argument("corrected_db", type=float)
    correct.add_argument("--learned-day", dest="learned_day", type=int, required=True)
    correct.add_argument("--reason", default="calibration", choices=["calibration", "drift", "repair", "other"])
    correct.add_argument("--revision", default=None)
    correct.set_defaults(func=cmd_correct)

    sign = sub.add_parser("sign", help="record a signed commitment")
    sign.add_argument("remedy")
    sign.add_argument("--day", type=int, required=True)
    sign.add_argument("--note", default="")
    sign.set_defaults(func=cmd_sign)

    cancel = sub.add_parser("cancel", help="request cancellation of a commitment")
    cancel.add_argument("remedy")
    cancel.add_argument("--day", type=int, required=True)
    cancel.add_argument("--reason", default="")
    cancel.set_defaults(func=cmd_cancel)

    confirm = sub.add_parser("confirm-cancel", help="confirm a pending cancellation")
    confirm.add_argument("remedy")
    confirm.add_argument("--day", type=int, required=True)
    confirm.set_defaults(func=cmd_confirm_cancel)

    accept = sub.add_parser("accept", help="record an acceptance")
    accept.add_argument("remedy")
    accept.add_argument("--building", default=None)
    accept.add_argument("--units", type=float, required=True)
    accept.add_argument("--day", type=int, required=True)
    accept.add_argument("--night", type=int, action="append", help="claimed reduction night (repeatable)")
    accept.add_argument("--note", default="")
    accept.set_defaults(func=cmd_accept)

    budget = sub.add_parser("budget", help="adjust an area budget line")
    budget.add_argument("area")
    budget.add_argument("--delta", type=int, required=True, help="change in cents")
    budget.add_argument("--day", type=int, required=True)
    budget.add_argument("--reason", default="")
    budget.set_defaults(func=cmd_budget)

    snapshots = sub.add_parser("snapshots", help="list adopted report snapshots")
    snapshots.add_argument("--json", action="store_true")
    snapshots.set_defaults(func=cmd_snapshots)

    show = sub.add_parser("show", help="show an adopted snapshot")
    show.add_argument("snapshot_id")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    audit = sub.add_parser("audit", help="show the ledger audit trail")
    audit.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SourceError, StoreError, WorkflowError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
