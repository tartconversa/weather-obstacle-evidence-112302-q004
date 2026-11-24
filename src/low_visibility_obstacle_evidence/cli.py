"""命令行入口：分析、排程、封闭重评估。

运行方式（推荐先安装本包，避免依赖工作目录）::

    python -m low_visibility_obstacle_evidence analyze --data-dir data --out-dir out
    python -m low_visibility_obstacle_evidence plan    --data-dir data --out-dir out
    python -m low_visibility_obstacle_evidence reevaluate \\
        --data-dir data --plan-json out/retest_plan.json --as-of 2026-12-01T00:00Z

所有路径必须显式传入，CLI 不隐式读取当前目录之外的资料。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import reporting
from .analysis import MIN_VALID_RESULTS, build_analysis
from .io_csv import load_dataset, parse_utc
from .planner import mark_executed, plan_retest, reevaluate_plan
from .time_match import MatchConfig


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="low_visibility_obstacle_evidence",
        description="低能见度道路障碍规避能力的证据评估与补测计划工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", required=True,
                        help="输入资料目录（含 visibility/drives 等 CSV）")
    common.add_argument("--out-dir", required=True,
                        help="报告与 JSON 的输出目录")
    common.add_argument("--max-gap-hours", type=float, default=12.0,
                        help="观测-行程时间差匹配上限（小时，默认 12）")
    common.add_argument("--min-valid-results", type=int,
                        default=MIN_VALID_RESULTS,
                        help=f"事件率稳定所需最少有效结果（默认 "
                             f"{MIN_VALID_RESULTS}）")

    analyze = sub.add_parser(
        "analyze", parents=[common],
        help="导入资料并生成覆盖矩阵、口径统计与版本对比报告",
    )
    analyze.set_defaults(handler=_cmd_analyze)

    plan = sub.add_parser(
        "plan", parents=[common],
        help="基于证据缺口生成补测排程（含窗口争用与改选损失）",
    )
    plan.add_argument("--as-of", default=None,
                      help="排程评估时点（ISO 时间），用于判定封闭事件")
    plan.set_defaults(handler=_cmd_plan)

    reeval = sub.add_parser(
        "reevaluate",
        help="道路封闭或许可变化后重评估既有计划中尚未执行的条目",
    )
    reeval.add_argument("--data-dir", required=True)
    reeval.add_argument("--plan-json", required=True,
                        help="既有计划 JSON（plan 子命令产物）")
    reeval.add_argument("--out-dir", required=True)
    reeval.add_argument("--as-of", required=True,
                        help="重评估时点（ISO 时间）")
    reeval.add_argument("--mark-executed", default="",
                        help="逗号分隔的已执行目标编号，如 G-001,G-002")
    reeval.set_defaults(handler=_cmd_reevaluate)
    return parser


def _config_from_args(args) -> MatchConfig:
    if args.max_gap_hours <= 0:
        raise SystemExit("--max-gap-hours 必须为正数")
    return MatchConfig(max_gap_seconds=args.max_gap_hours * 3600)


def _cmd_analyze(args) -> int:
    dataset = load_dataset(args.data_dir)
    analysis = build_analysis(
        dataset, _config_from_args(args), args.min_valid_results
    )
    out_dir = Path(args.out_dir)
    reporting.write_json(reporting.analysis_to_json(analysis, dataset),
                         out_dir / "analysis.json")
    report = reporting.render_markdown_report(analysis, dataset)
    _write_text(out_dir / "analysis_report.md", report)
    _print_summary(analysis)
    return 0


def _cmd_plan(args) -> int:
    dataset = load_dataset(args.data_dir)
    analysis = build_analysis(
        dataset, _config_from_args(args), args.min_valid_results
    )
    as_of = (
        parse_utc(args.as_of, field="as_of", row_no=0, filename="--as-of")
        if args.as_of else None
    )
    plan = plan_retest(
        analysis.targets, dataset.windows,
        route_coverage=dataset.route_coverage,
        closures=dataset.closures, as_of=as_of,
    )
    out_dir = Path(args.out_dir)
    reporting.write_json(reporting.plan_to_dict(plan, dataset),
                         out_dir / "retest_plan.json")
    reporting.write_plan_csv(plan, out_dir / "retest_plan.csv")
    reporting.write_json(reporting.analysis_to_json(analysis, dataset),
                         out_dir / "analysis.json")
    _write_text(
        out_dir / "analysis_report.md",
        reporting.render_markdown_report(analysis, dataset, plan),
    )
    print(f"已排程 {len(plan.entries)} 个目标，"
          f"{len(plan.unmet)} 个目标无合规窗口；"
          f"封闭路线：{plan.closed_routes or '无'}")
    return 0


def _cmd_reevaluate(args) -> int:
    dataset = load_dataset(args.data_dir)
    plan_path = Path(args.plan_json)
    with plan_path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    _check_plan_inputs(payload, dataset)

    as_of = parse_utc(args.as_of, field="as_of", row_no=0,
                      filename="--as-of")
    prior = _plan_from_payload(payload)
    executed = {
        item.strip() for item in args.mark_executed.split(",") if item.strip()
    }
    if executed:
        prior = mark_executed(prior, executed)
    updated = reevaluate_plan(
        prior, dataset.windows, dataset.closures, as_of=as_of
    )
    out_dir = Path(args.out_dir)
    reporting.write_json(reporting.plan_to_dict(updated, dataset),
                         out_dir / "retest_plan.json")
    reporting.write_plan_csv(updated, out_dir / "retest_plan.csv")
    blocked = sum(1 for entry in updated.entries if entry.status == "blocked")
    print(f"重评估完成：受阻 {blocked} 条，已执行 "
          f"{sum(1 for e in updated.entries if e.status == 'executed')} 条，"
          f"其余待执行。已采集行程的历史结论未回改。")
    return 0


def _check_plan_inputs(payload: dict, dataset) -> None:
    """核对计划生成时的资料校验和，输入已变更时显式拒绝静默重评估。"""
    prior_files = payload.get("source_files", {})
    changed = [
        name for name, digest in prior_files.items()
        if dataset.source_files.get(name) != digest
    ]
    if changed:
        raise SystemExit(
            "计划所依据的资料已变更，拒绝直接重评估，请先重新运行 plan："
            + "、".join(changed)
        )


def _plan_from_payload(payload: dict):
    """从 JSON 重建 RetestPlan（仅恢复计划自身状态，窗口资料以当前输入为准）。"""
    from .models import PlanEntry
    from .planner import RetestPlan, UnmetTarget

    entries = [
        PlanEntry(
            target_id=row["target_id"], region=row["region"],
            band=row["band"], target_type=row["target_type"],
            window_id=row["window_id"], route_id=row["route_id"],
            sensor_config=row["sensor_config"], status=row["status"],
            gap_kind=row["gap_kind"], primary=row["primary"],
            coverage_loss_weight=row["coverage_loss_weight"],
            note=row.get("note", ""),
        )
        for row in payload.get("entries", [])
    ]
    unmet = [
        UnmetTarget(
            target_id=row["target_id"], region=row["region"],
            band=row["band"], target_type=row["target_type"],
            gap_kind=row["gap_kind"], reason=row["reason"],
            competing_targets=row.get("competing_targets", []),
            candidate_window_ids=row.get("candidate_window_ids", []),
        )
        for row in payload.get("unmet_targets", [])
    ]
    as_of = None
    if payload.get("as_of"):
        as_of = datetime.fromisoformat(payload["as_of"])
    return RetestPlan(
        entries=entries, unmet=unmet,
        window_load=payload.get("window_load", {}),
        closed_routes=payload.get("closed_routes", []), as_of=as_of,
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _print_summary(analysis) -> None:
    print("版本口径汇总：")
    for version, values in analysis.caliber.items():
        print(
            f"  {version}: 低能见度行程 {values['low_visibility_trips']}，"
            f"积水场景 {values['water_scenes']}，"
            f"有效规避结果 {values['valid_avoidance_results']}，"
            f"无法定档行程 {values['unknown_band_trips']}"
        )
    print(f"补测目标 {len(analysis.targets)} 个；"
          f"可外推版本对比 "
          f"{sum(1 for c in analysis.comparisons if c.extrapolatable)} / "
          f"{len(analysis.comparisons)} 个组合")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
