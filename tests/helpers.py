"""测试共用的最小资料构造工具。

所有合成记录都在测试内显式构造，不依赖 data/ 目录，保证单个测试
能独立表达业务场景（尤其是边界与异常路径）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from low_visibility_obstacle_evidence.models import (  # noqa: E402
    ClosureEvent,
    Dataset,
    Drive,
    ObstacleDetection,
    RouteCoverage,
    TestWindow,
    VisibilityObservation,
)

UTC = timezone.utc


def dt(hour: int, day: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def observation(sensor_id: str, region: str, moment: datetime,
                visibility_km: float, quality: str = "validated",
                coverage: float = 1.0) -> VisibilityObservation:
    return VisibilityObservation(
        sensor_id=sensor_id, region=region, observed_at=moment,
        visibility_km=visibility_km, quality=quality,
        coverage_fraction=coverage,
    )


def drive(trip_id: str, region: str = "R-N", version: str = "V15",
          start: datetime | None = None, end: datetime | None = None,
          sensor_config: str = "CFG-B",
          upload_fraction: float = 1.0) -> Drive:
    start = start or dt(8)
    end = end or (start + timedelta(hours=1))
    return Drive(
        trip_id=trip_id, version=version, vehicle_id="V-01",
        region=region, started_at=start, ended_at=end,
        sensor_config=sensor_config, upload_fraction=upload_fraction,
    )


def detection(obstacle_id: str, trip_id: str, frame_start: int,
              frame_end: int, *, type: str = "water",
              review_state: str = "pending",
              outcome: str = "intervention") -> ObstacleDetection:
    return ObstacleDetection(
        obstacle_id=obstacle_id, trip_id=trip_id,
        frame_start=frame_start, frame_end=frame_end, type=type,
        review_state=review_state, outcome=outcome,
    )


def window(window_id: str, region: str, route_id: str, band: str,
           sensor_config: str = "CFG-B", capacity: int = 1,
           status: str = "open", permit: str = "P-01") -> TestWindow:
    return TestWindow(
        window_id=window_id, region=region, route_id=route_id,
        visibility_band=band, permit=permit, sensor_config=sensor_config,
        capacity=capacity, status=status,
    )


def closure(route_id: str, region: str, closed_from: datetime,
            reason: str = "施工封闭",
            permit_after: str | None = None) -> ClosureEvent:
    return ClosureEvent(
        route_id=route_id, region=region, closed_from=closed_from,
        reason=reason, permit_after=permit_after,
    )


def route_weight(route_id: str, region: str, band: str,
                 weight: float, target_type: str = "water") -> RouteCoverage:
    return RouteCoverage(
        route_id=route_id, region=region, visibility_band=band,
        target_type=target_type, coverage_weight=weight,
    )


def dataset(**kwargs) -> Dataset:
    """构造只填充指定字段的资料集，其余为空列表。"""
    fields = (
        "visibility", "weather", "drives", "detections", "review_events",
        "windows", "closures", "route_coverage",
    )
    data = {name: [] for name in fields}
    data.update(kwargs)
    return Dataset(**data)
