"""时间错位匹配：把观测站能见度贴合到车辆行程。

观测站与车辆时钟存在偏差，且观测在整点、行程在整小时段，二者很少严格
对齐。本模块统一以“行程中点与观测时刻的绝对时间差”做就近匹配，并要求
时间差不超过可配置上限（默认 12 小时）。

关键口径：

* ``missing`` 质量的观测不能用于能见度定档——它是“观测缺失”这一业务
  事实，行程会被标记为 :data:`~low_visibility_obstacle_evidence.models.BAND_UNKNOWN`，
  而不是被悄悄归入某个分级。
* ``coverage_fraction < 1`` 表示观测只覆盖路线的一段，匹配结果带
  ``partial_coverage`` 标记，报告里必须与全程覆盖区分开。
* 地面观测无法定档时，允许使用气象站做兜底，但结论会带
  ``fallback_weather`` 痕迹，且只能作为弱证据。
* 超出时间差上限、或同区域没有任何观测时，行程定档为 unknown；
  unknown 行程不计入任何能见度分级的事件率分母。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    BAND_UNKNOWN,
    Drive,
    QUALITY_MISSING,
    TripVisibility,
    VisibilityObservation,
    WeatherObservation,
)

#: 默认允许的观测-行程时间差上限（秒）。
DEFAULT_MAX_GAP_SECONDS = 12 * 3600

#: 报告中提示“错位偏大但仍可用”的阈值（秒）。
LARGE_GAP_SECONDS = 6 * 3600


@dataclass
class MatchConfig:
    """匹配参数。"""

    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS


def _nearest(records, moment):
    """返回 (记录, 时间差秒)；并列时取观测时刻早、标识小者，保证确定性。"""
    candidates = [
        (record, abs((record.observed_at - moment).total_seconds()))
        for record in records
    ]
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (
        item[1], item[0].observed_at,
        getattr(item[0], "sensor_id", getattr(item[0], "station_id", "")),
    ))
    return candidates[0]


def match_trip_visibility(
    drive: Drive,
    visibility: list[VisibilityObservation],
    weather: list[WeatherObservation] | None = None,
    config: MatchConfig | None = None,
) -> TripVisibility:
    """为单条行程推导能见度定档结论。"""
    config = config or MatchConfig()
    moment = drive.midpoint
    regional = [obs for obs in visibility if obs.region == drive.region]
    nearest, gap = _nearest(regional, moment)

    if nearest is not None and gap is not None and gap <= config.max_gap_seconds:
        partial = nearest.coverage_fraction < 1.0
        if nearest.quality == QUALITY_MISSING:
            # 观测存在但质量缺失：不能定档，保留来源便于追溯。
            return TripVisibility(
                trip_id=drive.trip_id, band=BAND_UNKNOWN,
                matched_sensor_id=nearest.sensor_id, time_gap_seconds=gap,
                observation_quality=nearest.quality,
                partial_coverage=partial, within_gap_limit=True,
                sources=[f"visibility:{nearest.sensor_id}(missing)"],
            )
        return TripVisibility(
            trip_id=drive.trip_id, band=nearest.band,
            matched_sensor_id=nearest.sensor_id, time_gap_seconds=gap,
            observation_quality=nearest.quality,
            partial_coverage=partial, within_gap_limit=True,
            sources=[f"visibility:{nearest.sensor_id}"],
        )

    # 地面观测缺失或超出错位上限：尝试气象站兜底（弱证据）。
    if weather:
        regional_wx = [wx for wx in weather if wx.region == drive.region]
        nearest_wx, wx_gap = _nearest(regional_wx, moment)
        if (nearest_wx is not None and wx_gap is not None
                and wx_gap <= config.max_gap_seconds):
            from .models import classify_visibility
            return TripVisibility(
                trip_id=drive.trip_id,
                band=classify_visibility(nearest_wx.visibility_km),
                matched_sensor_id=None, time_gap_seconds=wx_gap,
                observation_quality=None,
                partial_coverage=nearest_wx.station_coverage < 1.0,
                within_gap_limit=True, fallback_weather=True,
                sources=[f"weather:{nearest_wx.station_id}"],
            )

    sensor_id = nearest.sensor_id if nearest is not None else None
    return TripVisibility(
        trip_id=drive.trip_id, band=BAND_UNKNOWN,
        matched_sensor_id=sensor_id, time_gap_seconds=gap,
        observation_quality=nearest.quality if nearest is not None else None,
        partial_coverage=False, within_gap_limit=False,
        sources=[],
    )


def match_all(
    drives: list[Drive],
    visibility: list[VisibilityObservation],
    weather: list[WeatherObservation] | None = None,
    config: MatchConfig | None = None,
) -> dict[str, TripVisibility]:
    """批量匹配，返回 ``trip_id -> TripVisibility``。"""
    return {
        drive.trip_id: match_trip_visibility(
            drive, visibility, weather, config
        )
        for drive in drives
    }


def skew_summary(matches: dict[str, TripVisibility]) -> dict[str, float]:
    """汇总错位情况，供报告说明时间对齐质量。"""
    gaps = [
        m.time_gap_seconds for m in matches.values()
        if m.time_gap_seconds is not None and m.within_gap_limit
    ]
    if not gaps:
        return {"matched": 0, "large_gap": 0, "max_gap_seconds": 0.0,
                "mean_gap_seconds": 0.0}
    return {
        "matched": len(gaps),
        "large_gap": sum(1 for gap in gaps if gap > LARGE_GAP_SECONDS),
        "max_gap_seconds": max(gaps),
        "mean_gap_seconds": sum(gaps) / len(gaps),
    }
