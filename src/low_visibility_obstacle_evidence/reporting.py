"""中文报告渲染与结构化导出。

输出刻意分成两份：

* Markdown 报告给分析团队与试验负责人阅读，所有口径、不可外推组合和
  证据缺口都用中文显式说明，不允许把缺证据写成“表现正常”。
* JSON 给下游系统导入，字段命名稳定、枚举值与模型常量一致。

补测计划另外导出 CSV，方便调度人员在表格工具中操作；计划 JSON 携带
资料文件校验和，封闭重评估时可核对输入是否已变更。
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from .models import (
    PLAN_EXECUTED,
    VISIBILITY_BANDS,
)
from .planner import RetestPlan

BAND_LABELS = {
    "restricted": "受限（<1km）",
    "low": "低能见度（1–5km）",
    "clear": "良好（≥5km）",
    "unknown": "无法定档",
}
GAP_LABELS = {
    "no_evidence": "无证据",
    "immature": "复核未成熟",
    "thin": "有效结果不足",
    "covered": "已覆盖",
}
STATUS_LABELS = {
    "proposed": "已排程（首选路线）",
    "rerouted": "已改选路线",
    "executed": "已执行",
    "blocked": "受阻待重排",
}


# ---------------------------------------------------------------------------
# JSON 序列化
# ---------------------------------------------------------------------------

def _default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"无法序列化类型: {type(value)!r}")


def analysis_to_json(analysis, dataset) -> dict:
    """把分析结论转为可 json.dump 的普通字典。"""
    return {
        "generated_at": _now_iso(),
        "config": analysis.config,
        "source_files": dataset.source_files,
        "versions": analysis.versions,
        "regions": analysis.regions,
        "time_skew": analysis.skew,
        "calibers": analysis.caliber,
        "coverage_matrix": [
            _cell_to_dict(cell)
            for cell in sorted(
                analysis.cells.values(),
                key=lambda c: (c.version, c.band, c.region),
            )
        ],
        "unknown_band_trips": [asdict(item)
                               for item in analysis.unknown_trips],
        "unclassified_scene_ids": analysis.unclassified_scene_ids,
        "comparisons": [asdict(entry) for entry in analysis.comparisons],
        "retest_targets": [asdict(target) for target in analysis.targets],
        "scenes": [asdict(scene) for scene in analysis.scenes],
    }


def _now_iso() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).isoformat()


def _cell_to_dict(cell) -> dict:
    rate = cell.observed_avoidance_rate
    return {
        "version": cell.version,
        "visibility_band": cell.band,
        "region": cell.region,
        "trip_count": cell.trip_count,
        "partial_coverage_trips": len(cell.partial_visibility_trip_ids),
        "low_upload_trips": len(cell.low_upload_trip_ids),
        "scene_count": len(cell.scene_ids),
        "mature_scene_count": len(cell.mature_scene_ids),
        "water_scene_count": len(cell.water_scene_ids),
        "valid_avoidance_results": cell.valid_avoidance_count,
        "avoided": len(cell.avoided_ids),
        "intervention": len(cell.intervention_ids),
        "observed_avoidance_rate": (
            round(rate, 4) if rate is not None else None
        ),
        "evidence_status": "observed" if cell.has_evidence
        else "no_evidence",
    }


def plan_to_dict(plan: RetestPlan, dataset) -> dict:
    return {
        "generated_at": _now_iso(),
        "as_of": plan.as_of.isoformat() if plan.as_of else None,
        "source_files": dataset.source_files,
        "closed_routes": plan.closed_routes,
        "entries": [asdict(entry) for entry in plan.entries],
        "unmet_targets": [asdict(item) for item in plan.unmet],
        "window_load": plan.window_load,
        "summary": {
            "total_targets": len(plan.entries) + len(plan.unmet),
            "scheduled": len(plan.entries),
            "rerouted": sum(1 for e in plan.entries
                            if e.status == "rerouted"),
            "blocked": sum(1 for e in plan.entries
                           if e.status == "blocked"),
            "executed": sum(1 for e in plan.entries
                            if e.status == PLAN_EXECUTED),
            "unmet": len(plan.unmet),
        },
    }


def write_json(payload: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2,
                  default=_default)
        stream.write("\n")


def write_plan_csv(plan: RetestPlan, path: str | Path) -> None:
    """导出调度人员使用的计划 CSV（UTF-8 BOM，便于表格软件打开）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "target_id", "region", "band", "target_type", "window_id",
        "route_id", "sensor_config", "status", "gap_kind", "primary",
        "coverage_loss_weight", "note",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for entry in plan.entries:
            row = asdict(entry)
            row["band"] = entry.band
            if row["coverage_loss_weight"] is None:
                row["coverage_loss_weight"] = ""
            writer.writerow(row)
        for item in plan.unmet:
            writer.writerow({
                "target_id": item.target_id, "region": item.region,
                "band": item.band, "target_type": item.target_type,
                "window_id": "", "route_id": "", "sensor_config": "",
                "status": "unmet", "gap_kind": item.gap_kind,
                "primary": "", "coverage_loss_weight": "",
                "note": item.reason,
            })


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------

def render_markdown_report(analysis, dataset, plan: RetestPlan | None = None) -> str:
    """渲染完整中文评估报告。"""
    parts: list[str] = []
    parts.append("# 低能见度道路障碍规避能力评估报告\n")
    parts.append(
        "> 本报告基于本地合成资料生成，不包含真实驾驶员、车辆或道路资料，"
        "也不引用任何新闻报道中的事故或故障结论。评估对象是公开功能说明"
        "所称“深积水等道路障碍规避改善”，结论仅在本资料覆盖范围内成立。\n"
    )

    parts.append("## 1. 资料来源与成熟度\n")
    parts.append(_sources_section(analysis, dataset))
    parts.append(_skew_section(analysis))

    parts.append("## 2. 口径说明（三类数字不可互换）\n")
    parts.append(_caliber_section(analysis))

    parts.append("## 3. 覆盖矩阵：版本 × 能见度分级 × 区域\n")
    parts.append(_matrix_section(analysis))

    parts.append("## 4. 无法定档与未归类记录\n")
    parts.append(_unknown_section(analysis))

    parts.append("## 5. 版本对比：可支持的结论与不可外推组合\n")
    parts.append(_comparison_section(analysis))

    parts.append("## 6. 证据缺口与补测目标\n")
    parts.append(_targets_section(analysis))

    if plan is not None:
        parts.append("## 7. 补测排程与共享窗口争用\n")
        parts.append(_plan_section(plan))

    parts.append("## 8. 解释边界\n")
    parts.append(_boundary_section(analysis))
    return "\n".join(parts) + "\n"


def _sources_section(analysis, dataset) -> str:
    lines = [
        "| 文件 | 记录数 | SHA-256（前 12 位） |",
        "| --- | ---: | --- |",
    ]
    counts = {
        "visibility.csv": len(dataset.visibility),
        "weather.csv": len(dataset.weather),
        "drives.csv": len(dataset.drives),
        "obstacles.csv": len(dataset.detections),
        "review_events.csv": len(dataset.review_events),
        "test_windows.csv": len(dataset.windows),
        "closures.csv": len(dataset.closures),
        "route_coverage.csv": len(dataset.route_coverage),
    }
    for filename, digest in sorted(dataset.source_files.items()):
        lines.append(
            f"| `{filename}` | {counts.get(filename, '—')} | "
            f"`{digest[:12]}` |"
        )
    optional_missing = [
        name for name in
        ("weather.csv", "review_events.csv", "closures.csv",
         "route_coverage.csv")
        if name not in dataset.source_files
    ]
    text = "\n".join(lines) + "\n"
    if optional_missing:
        text += (
            "\n未提供的可选资料："
            + "、".join(f"`{name}`" for name in optional_missing)
            + "。相关结论按无该资料处理，不做默认假设。\n"
        )
    return text


def _skew_section(analysis) -> str:
    skew = analysis.skew
    if not skew.get("matched"):
        return "\n没有任何行程成功匹配到观测，时间对齐无法评估。\n"
    return (
        f"\n匹配时间差上限：{analysis.config['max_gap_seconds'] / 3600:.0f} 小时。"
        f"成功匹配 {skew['matched']} 条行程，其中 {skew['large_gap']} 条"
        f"时间差超过 6 小时；平均错位 {skew['mean_gap_seconds'] / 3600:.1f} "
        f"小时，最大错位 {skew['max_gap_seconds'] / 3600:.1f} 小时。"
        "观测站与车辆时钟未做对时，所有定档均为就近匹配结论。\n"
    )


def _caliber_section(analysis) -> str:
    lines = [
        "三类口径的定义与本期数值如下，任何报告引用都必须保留口径名称：\n",
        "| 版本 | 低能见度行程数 | 其中部分覆盖 | 积水场景数 | "
        "复核成熟场景 | 有效规避结果 | 规避成功 | 人工介入 | 无法定档行程 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for version in analysis.versions:
        c = analysis.caliber[version]
        lines.append(
            f"| {version} | {c['low_visibility_trips']} | "
            f"{c['partial_coverage_trips']} | {c['water_scenes']} | "
            f"{c['mature_scenes']} | {c['valid_avoidance_results']} | "
            f"{c['avoided']} | {c['intervention']} | "
            f"{c['unknown_band_trips']} |"
        )
    lines.append(
        "\n- **低能见度行程数**：仅统计能被 validated/estimated 观测定档到 "
        "restricted/low 的行程；missing 质量与超错位上限的行程列入"
        "“无法定档”，不计入分母。"
    )
    lines.append(
        "- **积水场景数**：复核更正后仍成立的 water 场景；撤回与合并消失的"
        "检测不计，多帧重复已去重。"
    )
    lines.append(
        "- **有效规避结果**：复核 confirmed 且结果为 avoided/intervention 的"
        "场景；unclear 与未成熟场景不进入事件率。"
    )
    return "\n".join(lines) + "\n"


def _matrix_section(analysis) -> str:
    lines = [
        "| 版本 | 能见度分级 | 区域 | 行程数 | 部分覆盖行程 | "
        "低上传行程 | 积水场景 | 有效规避结果 | 观察规避率 | 证据状态 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for version in analysis.versions:
        for band in VISIBILITY_BANDS:
            for region in analysis.regions:
                cell = analysis.cells.get((version, band, region))
                lines.append(_matrix_row(version, band, region, cell))
    return "\n".join(lines) + (
        "\n\n证据状态为“无证据”的组合表示**没有任何行程覆盖**，"
        "不能解释为零事件或表现正常；“有行程无积水”同样不构成规避能力证据。\n"
    )


def _matrix_row(version, band, region, cell) -> str:
    label = BAND_LABELS[band]
    if cell is None:
        return (
            f"| {version} | {label} | {region} | 0 | 0 | 0 | 0 | 0 | "
            f"— | **无证据** |"
        )
    rate = cell.observed_avoidance_rate
    rate_text = f"{rate:.0%}" if rate is not None else "—"
    if not cell.has_evidence:
        status = "**无证据**"
    elif cell.valid_avoidance_count == 0:
        status = "有行程，无有效结果"
    else:
        status = "已观察"
    return (
        f"| {version} | {label} | {region} | {cell.trip_count} | "
        f"{len(cell.partial_visibility_trip_ids)} | "
        f"{len(cell.low_upload_trip_ids)} | "
        f"{len(cell.water_scene_ids)} | {cell.valid_avoidance_count} | "
        f"{rate_text} | {status} |"
    )


def _unknown_section(analysis) -> str:
    if not analysis.unknown_trips and not analysis.unclassified_scene_ids:
        return "本期所有行程均完成能见度定档，所有场景均可归入矩阵单元。\n"
    lines = []
    if analysis.unknown_trips:
        lines.append(
            "| 行程 | 版本 | 区域 | 无法定档原因 |",
        )
        lines.append("| --- | --- | --- | --- |")
        for item in analysis.unknown_trips:
            lines.append(
                f"| {item.trip_id} | {item.version} | {item.region} | "
                f"{item.reason} |"
            )
    if analysis.unclassified_scene_ids:
        lines.append(
            "\n以下场景所在行程无法定档能见度，**不进入任何分级的事件率"
            "分母**：" + "、".join(analysis.unclassified_scene_ids)
        )
    return "\n".join(lines) + "\n"


def _comparison_section(analysis) -> str:
    if len(analysis.versions) < 2:
        return "资料仅含单一版本，无法进行版本对比。\n"
    lines = [
        "| 能见度分级 | 区域 | 基线行程→候选行程 | 有效结果(基→候) | "
        "积水场景(基→候) | 规避率(基→候) | 差值 | 可否外推 | 限制说明 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in analysis.comparisons:
        rate_left = _pct(entry.rates[0])
        rate_right = _pct(entry.rates[1])
        diff = (
            f"{entry.rate_difference:+.0%}"
            if entry.rate_difference is not None else "—"
        )
        extrap = "是" if entry.extrapolatable else "**否**"
        lines.append(
            f"| {BAND_LABELS[entry.band]} | {entry.region} | "
            f"{entry.trip_counts[0]}→{entry.trip_counts[1]} | "
            f"{entry.valid_counts[0]}→{entry.valid_counts[1]} | "
            f"{entry.water_scene_counts[0]}→{entry.water_scene_counts[1]} | "
            f"{rate_left}→{rate_right} | {diff} | {extrap} | "
            f"{'；'.join(entry.reasons) or '—'} |"
        )
    return "\n".join(lines) + (
        "\n\n“可否外推=否”的组合只能陈述观察事实，不能据此宣称新版本"
        "规避能力改善或劣化。上传覆盖差异与部分路线覆盖已在限制说明中保留。\n"
    )


def _pct(value) -> str:
    return f"{value:.0%}" if value is not None else "—"


def _targets_section(analysis) -> str:
    if not analysis.targets:
        return (
            f"最新版本 {analysis.versions[-1] if analysis.versions else '—'} "
            "在全部能见度×区域组合均满足覆盖门槛，暂无补测目标。\n"
        )
    lines = [
        "| 目标编号 | 区域 | 能见度分级 | 缺口类型 | 现有行程 | "
        "成熟积水场景 | 有效结果 | 需要传感器配置 | 缺口原因 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for target in analysis.targets:
        lines.append(
            f"| {target.target_id} | {target.region} | "
            f"{BAND_LABELS[target.band]} | {GAP_LABELS[target.gap_kind]} | "
            f"{target.trip_count} | {target.mature_scene_count} | "
            f"{target.valid_avoidance_count} | "
            f"{target.required_sensor_config} | {target.reason} |"
        )
    return "\n".join(lines) + "\n"


def _plan_section(plan: RetestPlan) -> str:
    lines = []
    if plan.closed_routes:
        lines.append(
            "评估时点已封闭路线：" + "、".join(plan.closed_routes)
            + "。相关窗口不参与本轮排程。\n"
        )
    lines.append(
        "| 目标 | 窗口 | 路线 | 状态 | 缺口类型 | 首选路线 | "
        "覆盖损失权重 | 备注 |",
    )
    lines.append("| --- | --- | --- | --- | --- | --- | ---: | --- |")
    for entry in plan.entries:
        loss = (
            "无法量化" if entry.coverage_loss_weight is None
            else f"{entry.coverage_loss_weight:.2f}"
        )
        lines.append(
            f"| {entry.target_id} | {entry.window_id} | {entry.route_id} | "
            f"{STATUS_LABELS.get(entry.status, entry.status)} | "
            f"{GAP_LABELS[entry.gap_kind]} | "
            f"{'是' if entry.primary else '否'} | {loss} | {entry.note or '—'} |"
        )
    if plan.unmet:
        lines.append("\n**未满足目标（共享窗口争用或无合规窗口）：**\n")
        lines.append("| 目标 | 区域 | 分级 | 缺口类型 | 原因 | 争用对象 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in plan.unmet:
            lines.append(
                f"| {item.target_id} | {item.region} | "
                f"{BAND_LABELS[item.band]} | {GAP_LABELS[item.gap_kind]} | "
                f"{item.reason} | "
                f"{'、'.join(item.competing_targets) or '—'} |"
            )
    lines.append(
        "\n窗口容量利用："
        + "；".join(
            f"{wid} {info['used']}/{info['capacity']}"
            for wid, info in sorted(plan.window_load.items())
            if info["used"]
        )
    )
    return "\n".join(lines) + "\n"


def _boundary_section(analysis) -> str:
    return (
        "1. 报告中的规避率是**观察到的事件率**，分母仅含复核确认且结果"
        "确定的场景，不代表全量行程的概率。\n"
        "2. 标记为无证据、无法定档、复核未成熟的组合，均不得外推为"
        "“表现正常”或“零事故”。\n"
        "3. 版本对比仅在两侧均有足够有效结果时可外推；其余组合只陈述"
        "样本事实。\n"
        "4. 道路封闭或许可变更只影响尚未执行的补测条目；已采集行程按"
        "当时观测解释，不被后续事件回改。\n"
        f"5. 时间匹配上限为 {analysis.config['max_gap_seconds'] / 3600:.0f} "
        "小时，超过上限的行程不做能见度定档。\n"
        "6. 本资料为固定种子合成数据，仅供数据流程开发与测试，不对应"
        "任何真实车辆或公开事故报道。\n"
    )
