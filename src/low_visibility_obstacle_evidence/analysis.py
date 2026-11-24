"""覆盖矩阵、口径统计与版本对比。

本模块把三条容易混淆的口径严格分开，并在数据结构上保证无法互换：

1. **低能见度行程数**：行程中点能被可靠观测（validated/estimated）
   定档到 restricted/low 的行程数；missing 质量与超错位上限的行程进入
   unknown 清单，不进分母。
2. **积水场景数**：复核更正后 ``type == water`` 且仍然成立的场景数；
   撤回、合并消失的检测不计，多帧重复检测已在场景层去重。
3. **有效规避结果数**：复核确认真实、且结果为 avoided/intervention 的
   场景数；unclear、pending、revised 都不进入事件率分母。

没有覆盖的组合显式标记为 ``no_evidence``，与“观测到零事件”严格区分，
避免把没测过的区域报告成表现正常。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    BAND_UNKNOWN,
    CoverageCell,
    Drive,
    GAP_COVERED,
    GAP_IMMATURE,
    GAP_NO_EVIDENCE,
    GAP_THIN,
    GAP_PRIORITY,
    OUTCOME_AVOIDED,
    OUTCOME_INTERVENTION,
    REVIEW_CONFIRMED,
    RetestTarget,
    Scene,
    TripVisibility,
    TYPE_WATER,
    VISIBILITY_BANDS,
)
from .review import apply_review
from .time_match import MatchConfig, match_all, skew_summary

#: 单元内有效规避结果低于该值时，事件率不具统计稳定性，按 thin 处理。
MIN_VALID_RESULTS = 3

#: 低能见度分级集合（restricted 与 low）。
LOW_VISIBILITY_BANDS = ("restricted", "low")


@dataclass
class UnknownTrip:
    """无法定档能见度的行程及原因。"""

    trip_id: str
    version: str
    region: str
    reason: str


@dataclass
class ComparisonEntry:
    """两个版本在同一（分级 × 区域）组合下的对比结论。"""

    band: str
    region: str
    versions: tuple[str, str]
    trip_counts: tuple[int, int]
    valid_counts: tuple[int, int]
    water_scene_counts: tuple[int, int]
    rates: tuple[float | None, float | None]
    rate_difference: float | None
    extrapolatable: bool
    reasons: list[str] = field(default_factory=list)
    upload_fractions: tuple[float | None, float | None] = (None, None)


@dataclass
class AnalysisResult:
    """一次完整分析的全部结构化结论。"""

    matches: dict[str, TripVisibility]
    scenes: list[Scene]
    cells: dict[tuple[str, str, str], CoverageCell]
    versions: list[str]
    regions: list[str]
    unknown_trips: list[UnknownTrip]
    unclassified_scene_ids: list[str]
    withdrawn_scene_ids: list[str]
    skew: dict[str, float]
    caliber: dict[str, dict[str, int]]
    comparisons: list[ComparisonEntry]
    targets: list[RetestTarget]
    config: dict[str, object]


def _version_key(version: str) -> tuple:
    """版本排序：V14 < V15；非数字后缀按文本兜底。"""
    head = version[1:] if version[:1] in ("V", "v") else version
    try:
        return (0, int(head))
    except ValueError:
        return (1, head)


def build_analysis(dataset, config: MatchConfig | None = None,
                   min_valid_results: int = MIN_VALID_RESULTS) -> AnalysisResult:
    """执行匹配、场景重建、矩阵统计、版本对比与缺口识别。"""
    config = config or MatchConfig()
    drives: list[Drive] = dataset.drives
    matches = match_all(drives, dataset.visibility, dataset.weather, config)
    scenes = apply_review(dataset.detections, dataset.review_events)

    drive_by_id = {drive.trip_id: drive for drive in drives}
    scene_versions = {
        scene.scene_id: drive_by_id[scene.trip_id].version
        for scene in scenes
        if scene.trip_id in drive_by_id
    }
    versions = sorted({drive.version for drive in drives}, key=_version_key)
    # 区域集合必须包含观测站与窗口覆盖到、但车队从未行驶的区域——
    # 否则零行程区域的证据缺口会被漏掉。
    regions = sorted(
        {drive.region for drive in drives}
        | {obs.region for obs in dataset.visibility}
        | {window.region for window in dataset.windows}
        | {closure.region for closure in dataset.closures}
    )

    cells: dict[tuple[str, str, str], CoverageCell] = {}
    unknown_trips: list[UnknownTrip] = []

    for drive in drives:
        match = matches[drive.trip_id]
        if match.band == BAND_UNKNOWN:
            reason = _unknown_reason(match)
            unknown_trips.append(UnknownTrip(
                trip_id=drive.trip_id, version=drive.version,
                region=drive.region, reason=reason,
            ))
            continue
        cell = _cell(cells, drive.version, match.band, drive.region)
        cell.trip_ids.append(drive.trip_id)
        if match.partial_coverage:
            cell.partial_visibility_trip_ids.append(drive.trip_id)
        if not drive.full_upload:
            cell.low_upload_trip_ids.append(drive.trip_id)

    unclassified: list[str] = []
    withdrawn_scene_ids: list[str] = []
    for scene in scenes:
        if not scene.effective:
            # 撤回场景是审计事实，但不进入任何覆盖统计与事件率。
            withdrawn_scene_ids.append(scene.scene_id)
            continue
        drive = drive_by_id.get(scene.trip_id)
        if drive is None:  # 导入层外键校验已保证，仅防御
            continue
        match = matches[scene.trip_id]
        if match.band == BAND_UNKNOWN:
            unclassified.append(scene.scene_id)
            continue
        cell = _cell(cells, drive.version, match.band, drive.region)
        cell.scene_ids.append(scene.scene_id)
        if scene.review_mature:
            cell.mature_scene_ids.append(scene.scene_id)
        if scene.type == TYPE_WATER:
            cell.water_scene_ids.append(scene.scene_id)
        if scene.valid_avoidance_result:
            cell.valid_avoidance_ids.append(scene.scene_id)
            if scene.outcome == OUTCOME_AVOIDED:
                cell.avoided_ids.append(scene.scene_id)
            elif scene.outcome == OUTCOME_INTERVENTION:
                cell.intervention_ids.append(scene.scene_id)

    caliber = _caliber_summary(
        versions, cells, unknown_trips, scenes, scene_versions
    )
    comparisons = _compare_versions(versions, regions, cells, drives)
    targets = _build_targets(
        dataset, versions, regions, cells, min_valid_results
    )

    return AnalysisResult(
        matches=matches, scenes=scenes, cells=cells, versions=versions,
        regions=regions, unknown_trips=unknown_trips,
        unclassified_scene_ids=sorted(unclassified),
        withdrawn_scene_ids=sorted(withdrawn_scene_ids),
        skew=skew_summary(matches),
        caliber=caliber, comparisons=comparisons, targets=targets,
        config={
            "max_gap_seconds": config.max_gap_seconds,
            "min_valid_results": min_valid_results,
        },
    )


def _unknown_reason(match: TripVisibility) -> str:
    if not match.within_gap_limit:
        return "观测时间差超出匹配上限或同区域无观测"
    if match.observation_quality == "missing":
        return "最近观测质量为 missing，不能用于能见度定档"
    return "缺乏可定档的可靠观测"


def _cell(cells: dict[tuple[str, str, str], CoverageCell],
          version: str, band: str, region: str) -> CoverageCell:
    key = (version, band, region)
    if key not in cells:
        cells[key] = CoverageCell(version=version, band=band, region=region)
    return cells[key]


def _caliber_summary(versions, cells, unknown_trips, scenes,
                     scene_versions) -> dict:
    """三口径分项汇总，键名直接对应业务术语，避免二次解释。"""
    summary: dict[str, dict[str, int]] = {}
    unknown_by_version: dict[str, int] = {}
    for item in unknown_trips:
        unknown_by_version[item.version] = \
            unknown_by_version.get(item.version, 0) + 1

    for version in versions:
        low_trips = 0
        partial_trips = 0
        water_scenes = 0
        valid_results = 0
        avoided = 0
        intervention = 0
        mature = 0
        for (v, band, _region), cell in cells.items():
            if v != version:
                continue
            if band in LOW_VISIBILITY_BANDS:
                low_trips += cell.trip_count
                partial_trips += len(cell.partial_visibility_trip_ids)
            water_scenes += len(cell.water_scene_ids)
            valid_results += cell.valid_avoidance_count
            avoided += len(cell.avoided_ids)
            intervention += len(cell.intervention_ids)
            mature += len(cell.mature_scene_ids)
        effective_total = sum(
            1 for scene in scenes
            if scene.effective
            and scene_versions.get(scene.scene_id) == version
        )
        withdrawn_total = sum(
            1 for scene in scenes
            if not scene.effective
            and scene_versions.get(scene.scene_id) == version
        )
        summary[version] = {
            "low_visibility_trips": low_trips,
            "partial_coverage_trips": partial_trips,
            "water_scenes": water_scenes,
            "mature_scenes": mature,
            "valid_avoidance_results": valid_results,
            "avoided": avoided,
            "intervention": intervention,
            "unknown_band_trips": unknown_by_version.get(version, 0),
            "total_scenes": effective_total,
            "withdrawn_scenes": withdrawn_total,
        }
    return summary


def _mean_upload(drives: list[Drive], trip_ids: list[str]) -> float | None:
    if not trip_ids:
        return None
    by_id = {d.trip_id: d.upload_fraction for d in drives}
    values = [by_id[t] for t in trip_ids if t in by_id]
    return sum(values) / len(values) if values else None


def _compare_versions(versions, regions, cells, drives) -> list[ComparisonEntry]:
    if len(versions) < 2:
        return []
    baseline, candidate = versions[0], versions[-1]
    bands = list(VISIBILITY_BANDS)
    entries: list[ComparisonEntry] = []

    for band in bands:
        for region in regions:
            left = cells.get((baseline, band, region))
            right = cells.get((candidate, band, region))
            entry = _comparison_entry(
                baseline, candidate, band, region, left, right, drives
            )
            entries.append(entry)
    return entries


def _comparison_entry(baseline, candidate, band, region,
                      left: CoverageCell | None,
                      right: CoverageCell | None,
                      drives) -> ComparisonEntry:
    left = left or CoverageCell(version=baseline, band=band, region=region)
    right = right or CoverageCell(version=candidate, band=band, region=region)
    reasons: list[str] = []

    if not left.has_evidence:
        reasons.append(f"基线版本 {baseline} 在该组合无行程，无法外推")
    if not right.has_evidence:
        reasons.append(f"候选版本 {candidate} 在该组合无行程，无法外推")
    if left.has_evidence and left.valid_avoidance_count == 0:
        reasons.append(f"{baseline} 无有效规避结果，事件率不可计算")
    if right.has_evidence and right.valid_avoidance_count == 0:
        reasons.append(f"{candidate} 无有效规避结果，事件率不可计算")

    rate_left = left.observed_avoidance_rate
    rate_right = right.observed_avoidance_rate
    difference = (
        rate_right - rate_left
        if rate_left is not None and rate_right is not None
        else None
    )
    extrapolatable = difference is not None
    if extrapolatable and (
        left.valid_avoidance_count < MIN_VALID_RESULTS
        or right.valid_avoidance_count < MIN_VALID_RESULTS
    ):
        extrapolatable = False
        reasons.append("至少一侧有效规避结果少于 "
                       f"{MIN_VALID_RESULTS} 个，差异不具统计稳定性")

    if left.low_upload_trip_ids or right.low_upload_trip_ids:
        reasons.append("存在上传不完整行程，事件捕获可能遗漏")
    if left.partial_visibility_trip_ids or right.partial_visibility_trip_ids:
        reasons.append("能见度观测仅覆盖部分路线")

    return ComparisonEntry(
        band=band, region=region, versions=(baseline, candidate),
        trip_counts=(left.trip_count, right.trip_count),
        valid_counts=(left.valid_avoidance_count,
                      right.valid_avoidance_count),
        water_scene_counts=(len(left.water_scene_ids),
                            len(right.water_scene_ids)),
        rates=(rate_left, rate_right), rate_difference=difference,
        extrapolatable=extrapolatable, reasons=reasons,
        upload_fractions=(
            _mean_upload(drives, left.trip_ids),
            _mean_upload(drives, right.trip_ids),
        ),
    )


def _build_targets(dataset, versions, regions, cells,
                   min_valid_results: int) -> list[RetestTarget]:
    """为最新版本识别积水规避能力的证据缺口并生成补测目标。"""
    if not versions:
        return []
    candidate = versions[-1]
    drives = dataset.drives
    targets: list[RetestTarget] = []
    seq = 1

    config_by_region = _dominant_config(drives, candidate)
    fallback_config = _fallback_config(dataset, candidate)

    for region in regions:
        for band in VISIBILITY_BANDS:
            cell = cells.get((candidate, band, region))
            gap, reason = _classify_gap(cell, min_valid_results)
            if gap == GAP_COVERED:
                continue
            targets.append(RetestTarget(
                target_id=f"G-{seq:03d}",
                region=region, band=band, target_type=TYPE_WATER,
                version=candidate, gap_kind=gap, reason=reason,
                required_sensor_config=config_by_region.get(
                    region, fallback_config
                ),
                trip_count=cell.trip_count if cell else 0,
                mature_scene_count=(len(cell.mature_scene_ids)
                                    if cell else 0),
                valid_avoidance_count=(cell.valid_avoidance_count
                                       if cell else 0),
            ))
            seq += 1

    targets.sort(
        key=lambda t: (-GAP_PRIORITY[t.gap_kind], t.region, t.band)
    )
    # 重排后重新编号，保证目标编号与优先级顺序一致。
    for index, target in enumerate(targets, start=1):
        object.__setattr__(target, "target_id", f"G-{index:03d}")
    return targets


def _classify_gap(cell: CoverageCell | None,
                  min_valid_results: int) -> tuple[str, str]:
    if cell is None or cell.trip_count == 0:
        return GAP_NO_EVIDENCE, "该版本在此能见度×区域组合没有任何行程"
    if len(cell.water_scene_ids) == 0:
        return (
            GAP_NO_EVIDENCE,
            "有行程但未观测到任何积水场景，无法支持规避能力结论",
        )
    if len(cell.mature_scene_ids) == 0:
        return GAP_IMMATURE, "积水场景均未完成人工复核，结论不成熟"
    if cell.valid_avoidance_count < min_valid_results:
        return (
            GAP_THIN,
            f"有效规避结果 {cell.valid_avoidance_count} 个，"
            f"少于稳定评估所需的 {min_valid_results} 个",
        )
    return GAP_COVERED, ""


def _dominant_config(drives, version) -> dict[str, str]:
    votes: dict[str, dict[str, int]] = {}
    for drive in drives:
        if drive.version != version:
            continue
        votes.setdefault(drive.region, {})
        votes[drive.region][drive.sensor_config] = \
            votes[drive.region].get(drive.sensor_config, 0) + 1
    return {
        region: sorted(configs.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for region, configs in votes.items()
    }


def _fallback_config(dataset, version: str) -> str:
    configs = {
        drive.sensor_config for drive in dataset.drives
        if drive.version == version
    }
    return sorted(configs)[0] if configs else "CFG-B"
