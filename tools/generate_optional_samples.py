"""生成可选资料样例：复核事件、道路封闭、路线覆盖权重。

基线四个 CSV 由资料契约固定，本脚本只新增三个可选文件，全部为确定性
合成值，供应用演示与测试使用。重复运行结果一致。
"""

from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


REVIEW_EVENTS = [
    # O-003 改判结果：避免成功改判为人工介入（结论被更正）
    ("RV-001", "O-003", "2026-11-21T09:00Z", "reclassify", "",
     "intervention", "", "", "", "复核录像确认需人工接管"),
    # O-006 深积水确认
    ("RV-002", "O-006", "2026-11-21T09:30Z", "confirm", "water",
     "avoided", "", "", "", ""),
    # O-009 撤回：传感器误检
    ("RV-003", "O-009", "2026-11-21T10:00Z", "reject", "", "",
     "", "", "", "路面反光导致误检"),
    # O-012 边界拆分，只保留后半段为真实场景
    ("RV-004", "O-012", "2026-11-21T10:30Z", "split", "", "",
     "", "122", "124", "前两帧属于另一障碍"),
    # O-015 确认并合并 O-018（同一积水段多帧重复检测）
    ("RV-005", "O-015", "2026-11-21T11:00Z", "confirm", "water",
     "avoided", "", "", "", ""),
    ("RV-006", "O-018", "2026-11-21T11:05Z", "merge", "", "",
     "O-015", "", "", "与 O-015 为同一积水场景"),
    # O-021 类型更正：动物误标改为深积水
    ("RV-007", "O-021", "2026-11-21T11:30Z", "reclassify", "water",
     "avoided", "", "", "", "原始类型标注错误"),
    # O-024 撤回
    ("RV-008", "O-024", "2026-11-21T12:00Z", "reject", "", "",
     "", "", "", "重复检测，无独立障碍"),
    # O-027 结果存疑，确认场景但结果不清晰
    ("RV-009", "O-027", "2026-11-21T12:30Z", "confirm", "water",
     "unclear", "", "", "", "遮挡严重，规避动作无法定性"),
    # O-030 确认
    ("RV-010", "O-030", "2026-11-21T13:00Z", "confirm", "water",
     "avoided", "", "", "", ""),
]

CLOSURES = [
    # RT-3 在 11 月底封闭，排程时若评估时点晚于此日期则窗口失效
    ("RT-3", "R-N", "2026-11-25T00:00Z", "跨线桥施工，双向封闭", ""),
]

# 路线对（能见度分级 × 深积水）的历史覆盖权重；RT-1 在低能见度下最强
ROUTE_COVERAGE = [
    ("RT-1", "R-S", "low", "water", 0.9),
    ("RT-1", "R-S", "restricted", "water", 0.6),
    ("RT-4", "R-S", "low", "water", 0.55),
    ("RT-4", "R-S", "restricted", "water", 0.4),
    ("RT-2", "R-W", "clear", "water", 0.8),
    ("RT-5", "R-W", "clear", "water", 0.5),
    ("RT-3", "R-N", "restricted", "water", 0.85),
    ("RT-0", "R-N", "restricted", "water", 0.45),
]


def write_review_events() -> None:
    columns = [
        "event_id", "obstacle_id", "reviewed_at", "action", "new_type",
        "new_outcome", "merge_into", "frame_start", "frame_end", "note",
    ]
    with (DATA / "review_events.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(REVIEW_EVENTS)


def write_closures() -> None:
    with (DATA / "closures.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["route_id", "region", "closed_from", "reason", "permit_after"]
        )
        writer.writerows(CLOSURES)


def write_route_coverage() -> None:
    with (DATA / "route_coverage.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["route_id", "region", "visibility_band", "target_type",
             "coverage_weight"]
        )
        writer.writerows(ROUTE_COVERAGE)


def main() -> None:
    write_review_events()
    write_closures()
    write_route_coverage()
    print("可选资料样例已写入", DATA)


if __name__ == "__main__":
    main()
