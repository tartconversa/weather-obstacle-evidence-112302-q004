"""CSV 导入层。

设计原则：

* 所有输入文件都通过显式路径传入，导入层不依赖当前工作目录，避免
  不可信数据目录中的同名模块（如 ``json.py``）被解释器加载。
* 缺失文件按“可选资料”处理：复核事件、道路封闭、路线覆盖、气象补充
  可以不存在，但四个基线文件必须存在。
* 编码固定为 UTF-8（兼容 BOM）；表头、枚举、数值与外键逐一校验，
  失败时抛出带文件名与行号的 ``ValueError``，不静默吞掉。
* 同时计算文件 SHA-256，写入报告的资料来源章节，保证复核可追溯。
"""

from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from . import models

REQUIRED_HEADERS = {
    "visibility.csv": [
        "sensor_id", "region", "observed_at", "visibility_km",
        "observation_quality", "coverage_fraction",
    ],
    "drives.csv": [
        "trip_id", "version", "vehicle_id", "region", "started_at",
        "ended_at", "sensor_config", "upload_fraction",
    ],
    "obstacles.csv": [
        "obstacle_id", "trip_id", "frame_start", "frame_end",
        "type", "review_state", "outcome",
    ],
    "test_windows.csv": [
        "window_id", "region", "route_id", "visibility_band", "permit",
        "sensor_config", "capacity", "status",
    ],
}

OPTIONAL_HEADERS = {
    "weather.csv": [
        "station_id", "region", "observed_at", "rain_mm",
        "visibility_km", "station_coverage",
    ],
    "review_events.csv": [
        "event_id", "obstacle_id", "reviewed_at", "action", "new_type",
        "new_outcome", "merge_into", "frame_start", "frame_end", "note",
    ],
    "closures.csv": [
        "route_id", "region", "closed_from", "reason", "permit_after",
    ],
    "route_coverage.csv": [
        "route_id", "region", "visibility_band", "target_type",
        "coverage_weight",
    ],
}

_BANDS = set(models.VISIBILITY_BANDS)
_QUALITIES = {
    models.QUALITY_VALIDATED, models.QUALITY_ESTIMATED, models.QUALITY_MISSING,
}
_TYPES = {models.TYPE_WATER, models.TYPE_DEBRIS, models.TYPE_ANIMAL}
_REVIEW_STATES = {
    models.REVIEW_PENDING, models.REVIEW_REVISED,
    models.REVIEW_CONFIRMED, models.REVIEW_WITHDRAWN,
}
_OUTCOMES = {
    models.OUTCOME_AVOIDED, models.OUTCOME_INTERVENTION, models.OUTCOME_UNCLEAR,
}
_WINDOW_STATUSES = {
    models.WINDOW_OPEN, models.WINDOW_HELD,
    models.WINDOW_ASSIGNED, models.WINDOW_CLOSED,
}
_REVIEW_ACTIONS = {"confirm", "reject", "reclassify", "merge", "split"}


def parse_utc(value: str, *, field: str, row_no: int, filename: str) -> datetime:
    """解析 ISO 8601 时间，统一归一化为带 UTC 时区的 datetime。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"{filename} 第 {row_no} 行字段 {field} 不是合法 ISO 时间: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_float(value: str, *, field: str, row_no: int, filename: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(
            f"{filename} 第 {row_no} 行字段 {field} 不是合法数值: {value!r}"
        ) from exc


def _parse_int(value: str, *, field: str, row_no: int, filename: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"{filename} 第 {row_no} 行字段 {field} 不是合法整数: {value!r}"
        ) from exc


def _check_header(filename: str, actual: list[str] | None, expected: list[str]) -> None:
    if actual != expected:
        raise ValueError(
            f"{filename} 表头不匹配。\n期望: {expected}\n实际: {actual}"
        )


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = list(reader.fieldnames or [])
        rows = list(reader)
    return headers, rows


def _check_enum(value: str, allowed: set[str], *, field: str,
                row_no: int, filename: str) -> str:
    if value not in allowed:
        raise ValueError(
            f"{filename} 第 {row_no} 行字段 {field} 取值 {value!r} 不在 "
            f"允许范围 {sorted(allowed)} 内"
        )
    return value


def _check_unique(ids: list[str], *, field: str, filename: str) -> None:
    seen: set[str] = set()
    for value in ids:
        if value in seen:
            raise ValueError(f"{filename} 中字段 {field} 出现重复标识: {value}")
        seen.add(value)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_visibility(path: Path) -> list[models.VisibilityObservation]:
    headers, rows = _read_rows(path)
    _check_header(path.name, headers, REQUIRED_HEADERS["visibility.csv"])
    records: list[models.VisibilityObservation] = []
    for i, row in enumerate(rows, start=2):
        quality = _check_enum(
            row["observation_quality"], _QUALITIES,
            field="observation_quality", row_no=i, filename=path.name,
        )
        coverage = _parse_float(
            row["coverage_fraction"], field="coverage_fraction",
            row_no=i, filename=path.name,
        )
        if not 0.0 < coverage <= 1.0:
            raise ValueError(
                f"{path.name} 第 {i} 行 coverage_fraction 必须在 (0,1] 内"
            )
        records.append(models.VisibilityObservation(
            sensor_id=row["sensor_id"],
            region=row["region"],
            observed_at=parse_utc(
                row["observed_at"], field="observed_at",
                row_no=i, filename=path.name,
            ),
            visibility_km=_parse_float(
                row["visibility_km"], field="visibility_km",
                row_no=i, filename=path.name,
            ),
            quality=quality,
            coverage_fraction=coverage,
        ))
    _check_unique([r.sensor_id + r.observed_at.isoformat() for r in records],
                  field="sensor_id+observed_at", filename=path.name)
    return records


def _load_weather(path: Path) -> list[models.WeatherObservation]:
    headers, rows = _read_rows(path)
    _check_header(path.name, headers, OPTIONAL_HEADERS["weather.csv"])
    records: list[models.WeatherObservation] = []
    for i, row in enumerate(rows, start=2):
        records.append(models.WeatherObservation(
            station_id=row["station_id"],
            region=row["region"],
            observed_at=parse_utc(
                row["observed_at"], field="observed_at",
                row_no=i, filename=path.name,
            ),
            rain_mm=_parse_float(row["rain_mm"], field="rain_mm",
                                 row_no=i, filename=path.name),
            visibility_km=_parse_float(row["visibility_km"],
                                       field="visibility_km",
                                       row_no=i, filename=path.name),
            station_coverage=_parse_float(row["station_coverage"],
                                          field="station_coverage",
                                          row_no=i, filename=path.name),
        ))
    return records


def _load_drives(path: Path) -> list[models.Drive]:
    headers, rows = _read_rows(path)
    _check_header(path.name, headers, REQUIRED_HEADERS["drives.csv"])
    records: list[models.Drive] = []
    for i, row in enumerate(rows, start=2):
        started = parse_utc(row["started_at"], field="started_at",
                            row_no=i, filename=path.name)
        ended = parse_utc(row["ended_at"], field="ended_at",
                          row_no=i, filename=path.name)
        if not started < ended:
            raise ValueError(
                f"{path.name} 第 {i} 行 started_at 必须早于 ended_at"
            )
        upload = _parse_float(row["upload_fraction"], field="upload_fraction",
                              row_no=i, filename=path.name)
        if not 0.0 < upload <= 1.0:
            raise ValueError(
                f"{path.name} 第 {i} 行 upload_fraction 必须在 (0,1] 内"
            )
        records.append(models.Drive(
            trip_id=row["trip_id"], version=row["version"],
            vehicle_id=row["vehicle_id"], region=row["region"],
            started_at=started, ended_at=ended,
            sensor_config=row["sensor_config"], upload_fraction=upload,
        ))
    _check_unique([r.trip_id for r in records],
                  field="trip_id", filename=path.name)
    return records


def _load_detections(path: Path) -> list[models.ObstacleDetection]:
    headers, rows = _read_rows(path)
    _check_header(path.name, headers, REQUIRED_HEADERS["obstacles.csv"])
    records: list[models.ObstacleDetection] = []
    for i, row in enumerate(rows, start=2):
        frame_start = _parse_int(row["frame_start"], field="frame_start",
                                 row_no=i, filename=path.name)
        frame_end = _parse_int(row["frame_end"], field="frame_end",
                               row_no=i, filename=path.name)
        if frame_start > frame_end:
            raise ValueError(
                f"{path.name} 第 {i} 行 frame_start 不能晚于 frame_end"
            )
        records.append(models.ObstacleDetection(
            obstacle_id=row["obstacle_id"], trip_id=row["trip_id"],
            frame_start=frame_start, frame_end=frame_end,
            type=_check_enum(row["type"], _TYPES, field="type",
                             row_no=i, filename=path.name),
            review_state=_check_enum(
                row["review_state"], _REVIEW_STATES,
                field="review_state", row_no=i, filename=path.name,
            ),
            outcome=_check_enum(row["outcome"], _OUTCOMES,
                                field="outcome", row_no=i,
                                filename=path.name),
        ))
    _check_unique([r.obstacle_id for r in records],
                  field="obstacle_id", filename=path.name)
    return records


def _load_review_events(path: Path) -> list[models.ReviewEvent]:
    headers, rows = _read_rows(path)
    _check_header(path.name, headers, OPTIONAL_HEADERS["review_events.csv"])
    records: list[models.ReviewEvent] = []

    def optional_text(value: str) -> str | None:
        value = value.strip()
        return value or None

    for i, row in enumerate(rows, start=2):
        action = _check_enum(row["action"], _REVIEW_ACTIONS,
                             field="action", row_no=i, filename=path.name)
        new_type = optional_text(row["new_type"])
        if new_type is not None:
            _check_enum(new_type, _TYPES, field="new_type",
                        row_no=i, filename=path.name)
        new_outcome = optional_text(row["new_outcome"])
        if new_outcome is not None:
            _check_enum(new_outcome, _OUTCOMES, field="new_outcome",
                        row_no=i, filename=path.name)
        records.append(models.ReviewEvent(
            event_id=row["event_id"], obstacle_id=row["obstacle_id"],
            reviewed_at=parse_utc(row["reviewed_at"], field="reviewed_at",
                                  row_no=i, filename=path.name),
            action=action, new_type=new_type, new_outcome=new_outcome,
            merge_into=optional_text(row["merge_into"]),
            frame_start=(int(row["frame_start"])
                         if row["frame_start"].strip() else None),
            frame_end=(int(row["frame_end"])
                       if row["frame_end"].strip() else None),
            note=row.get("note", ""),
        ))
    _check_unique([r.event_id for r in records],
                  field="event_id", filename=path.name)
    return records


def _load_windows(path: Path) -> list[models.TestWindow]:
    headers, rows = _read_rows(path)
    _check_header(path.name, headers, REQUIRED_HEADERS["test_windows.csv"])
    records: list[models.TestWindow] = []
    for i, row in enumerate(rows, start=2):
        capacity = _parse_int(row["capacity"], field="capacity",
                              row_no=i, filename=path.name)
        if capacity < 1:
            raise ValueError(
                f"{path.name} 第 {i} 行 capacity 必须 >= 1"
            )
        records.append(models.TestWindow(
            window_id=row["window_id"], region=row["region"],
            route_id=row["route_id"],
            visibility_band=_check_enum(
                row["visibility_band"], _BANDS, field="visibility_band",
                row_no=i, filename=path.name,
            ),
            permit=row["permit"], sensor_config=row["sensor_config"],
            capacity=capacity,
            status=_check_enum(row["status"], _WINDOW_STATUSES,
                               field="status", row_no=i,
                               filename=path.name),
        ))
    _check_unique([r.window_id for r in records],
                  field="window_id", filename=path.name)
    return records


def _load_closures(path: Path) -> list[models.ClosureEvent]:
    headers, rows = _read_rows(path)
    _check_header(path.name, headers, OPTIONAL_HEADERS["closures.csv"])
    records: list[models.ClosureEvent] = []
    for i, row in enumerate(rows, start=2):
        permit = row["permit_after"].strip()
        records.append(models.ClosureEvent(
            route_id=row["route_id"], region=row["region"],
            closed_from=parse_utc(row["closed_from"], field="closed_from",
                                  row_no=i, filename=path.name),
            reason=row["reason"], permit_after=permit or None,
        ))
    return records


def _load_route_coverage(path: Path) -> list[models.RouteCoverage]:
    headers, rows = _read_rows(path)
    _check_header(path.name, headers, OPTIONAL_HEADERS["route_coverage.csv"])
    records: list[models.RouteCoverage] = []
    for i, row in enumerate(rows, start=2):
        records.append(models.RouteCoverage(
            route_id=row["route_id"], region=row["region"],
            visibility_band=_check_enum(
                row["visibility_band"], _BANDS, field="visibility_band",
                row_no=i, filename=path.name,
            ),
            target_type=_check_enum(
                row["target_type"], _TYPES, field="target_type",
                row_no=i, filename=path.name,
            ),
            coverage_weight=_parse_float(
                row["coverage_weight"], field="coverage_weight",
                row_no=i, filename=path.name,
            ),
        ))
    return records


def load_dataset(data_dir: str | Path) -> models.Dataset:
    """从资料目录导入全部可用文件并完成跨文件外键校验。"""
    base = Path(data_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"资料目录不存在: {base}")

    dataset = models.Dataset()

    for filename in REQUIRED_HEADERS:
        path = base / filename
        if not path.exists():
            raise FileNotFoundError(f"缺少必需资料文件: {path}")
        dataset.source_files[filename] = sha256_of(path)

    dataset.visibility = _load_visibility(base / "visibility.csv")
    dataset.drives = _load_drives(base / "drives.csv")
    dataset.detections = _load_detections(base / "obstacles.csv")
    dataset.windows = _load_windows(base / "test_windows.csv")

    optional_loaders = {
        "weather.csv": _load_weather,
        "review_events.csv": _load_review_events,
        "closures.csv": _load_closures,
        "route_coverage.csv": _load_route_coverage,
    }
    for filename, loader in optional_loaders.items():
        path = base / filename
        if path.exists():
            dataset.source_files[filename] = sha256_of(path)
            setattr(dataset, {
                "weather.csv": "weather",
                "review_events.csv": "review_events",
                "closures.csv": "closures",
                "route_coverage.csv": "route_coverage",
            }[filename], loader(path))

    _validate_references(dataset)
    return dataset


def _validate_references(dataset: models.Dataset) -> None:
    """跨文件外键校验：检测必须挂在已知行程下，复核指向已知检测。"""
    drive_ids = {drive.trip_id for drive in dataset.drives}
    for detection in dataset.detections:
        if detection.trip_id not in drive_ids:
            raise ValueError(
                f"obstacles.csv 中 {detection.obstacle_id} 引用了未知行程 "
                f"{detection.trip_id}"
            )
    detection_ids = {d.obstacle_id for d in dataset.detections}
    for event in dataset.review_events:
        if event.obstacle_id not in detection_ids:
            raise ValueError(
                f"review_events.csv 中 {event.event_id} 引用了未知检测 "
                f"{event.obstacle_id}"
            )
        if event.merge_into is not None and \
                event.merge_into not in detection_ids:
            raise ValueError(
                f"review_events.csv 中 {event.event_id} 的 merge_into 指向 "
                f"未知检测 {event.merge_into}"
            )
    drive_regions = {drive.region for drive in dataset.drives}
    observation_regions = {obs.region for obs in dataset.visibility}
    unknown_regions = observation_regions - drive_regions
    if unknown_regions:
        raise ValueError(
            "visibility.csv 出现 drives.csv 中不存在的区域: "
            f"{sorted(unknown_regions)}"
        )
