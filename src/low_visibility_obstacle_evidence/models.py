"""领域模型：能见度观测、车辆行程、障碍场景、复核事件与补测窗口。

本模块只承载数据结构与取值常量，不含匹配或统计逻辑，便于在测试中
手工构造数据而不必经过 CSV 导入层。

所有时间均为带 UTC 时区的 ``datetime``；CSV 中的 ``Z`` 后缀在导入层
统一解析。能见度分级阈值集中在 :data:`VISIBILITY_BANDS`，避免分析与
计划模块各自定义口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# ---------------------------------------------------------------------------
# 取值常量
# ---------------------------------------------------------------------------

#: 能见度分级（含上界，单位 km）。restricted：<1，low：[1,5)，clear：>=5。
VISIBILITY_BANDS = ("restricted", "low", "clear")
BAND_RESTRICTED = "restricted"
BAND_LOW = "low"
BAND_CLEAR = "clear"
BAND_UNKNOWN = "unknown"

#: 观测质量；missing 不参与能见度定档，只能作为“无法定档”的证据。
QUALITY_VALIDATED = "validated"
QUALITY_ESTIMATED = "estimated"
QUALITY_MISSING = "missing"
RELIABLE_QUALITIES = (QUALITY_VALIDATED, QUALITY_ESTIMATED)

#: 原始资料中的复核状态（复核事件落库前）。
REVIEW_PENDING = "pending"
REVIEW_REVISED = "revised"
REVIEW_CONFIRMED = "confirmed"
REVIEW_WITHDRAWN = "withdrawn"

#: 障碍/场景类型。深积水是本次版本说明的关注对象。
TYPE_WATER = "water"
TYPE_DEBRIS = "debris"
TYPE_ANIMAL = "animal"

#: 规避结果。unclear 属于已复核但非确定性结果。
OUTCOME_AVOIDED = "avoided"
OUTCOME_INTERVENTION = "intervention"
OUTCOME_UNCLEAR = "unclear"

#: 窗口状态：open 可排程；held/assigned 已占用；closed 为封闭状态。
WINDOW_OPEN = "open"
WINDOW_HELD = "held"
WINDOW_ASSIGNED = "assigned"
WINDOW_CLOSED = "closed"

#: 上传覆盖分级阈值（完整 / 部分）。
UPLOAD_FULL_THRESHOLD = 0.9

#: 计划条目状态。
PLAN_PROPOSED = "proposed"
PLAN_REROUTED = "rerouted"
PLAN_EXECUTED = "executed"
PLAN_BLOCKED = "blocked"

#: 证据缺口分级（数值越大越优先补测）。
GAP_NO_EVIDENCE = "no_evidence"
GAP_IMMATURE = "immature"
GAP_THIN = "thin"
GAP_COVERED = "covered"
GAP_PRIORITY = {
    GAP_NO_EVIDENCE: 3,
    GAP_IMMATURE: 2,
    GAP_THIN: 1,
    GAP_COVERED: 0,
}


class VisibilityBandError(ValueError):
    """能见度数值无法落到任何分级时抛出。"""


def classify_visibility(visibility_km: float) -> str:
    """按统一阈值把连续能见度换算成分级。

    >>> classify_visibility(0.5)
    'restricted'
    >>> classify_visibility(2)
    'low'
    >>> classify_visibility(10)
    'clear'
    """
    if visibility_km < 1.0:
        return BAND_RESTRICTED
    if visibility_km < 5.0:
        return BAND_LOW
    return BAND_CLEAR


# ---------------------------------------------------------------------------
# 输入记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisibilityObservation:
    """地面观测站一条能见度观测。

    ``coverage_fraction`` 表示该观测对辖区路网的空间覆盖比例；小于 1
    时只能解释路线的一段，匹配结果必须携带部分覆盖标记。
    """

    sensor_id: str
    region: str
    observed_at: datetime
    visibility_km: float
    quality: str
    coverage_fraction: float

    @property
    def band(self) -> str:
        return classify_visibility(self.visibility_km)

    @property
    def reliable(self) -> bool:
        return self.quality in RELIABLE_QUALITIES


@dataclass(frozen=True)
class WeatherObservation:
    """气象站补充观测，仅用于背景佐证，不单独决定能见度定档。"""

    station_id: str
    region: str
    observed_at: datetime
    rain_mm: float
    visibility_km: float
    station_coverage: float


@dataclass(frozen=True)
class Drive:
    """一条车辆行程及其上传覆盖事实。"""

    trip_id: str
    version: str
    vehicle_id: str
    region: str
    started_at: datetime
    ended_at: datetime
    sensor_config: str
    upload_fraction: float

    @property
    def midpoint(self) -> datetime:
        return self.started_at + (self.ended_at - self.started_at) / 2

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()

    @property
    def full_upload(self) -> bool:
        return self.upload_fraction >= UPLOAD_FULL_THRESHOLD


@dataclass(frozen=True)
class ObstacleDetection:
    """原始障碍检测记录（一个检测可横跨多个传感器帧）。"""

    obstacle_id: str
    trip_id: str
    frame_start: int
    frame_end: int
    type: str
    review_state: str
    outcome: str


@dataclass(frozen=True)
class ReviewEvent:
    """人工复核事件流中的一条更正。

    action 取值：
      - ``confirm``   确认为真实场景，可改写类型/结果
      - ``reject``    撤回误检
      - ``reclassify`` 改变场景类型或结果（场景仍成立）
      - ``merge``     并入 ``merge_into`` 指向的检测
      - ``split``     用 ``frame_end`` 收窄边界（原始记录的一段）
    """

    event_id: str
    obstacle_id: str
    reviewed_at: datetime
    action: str
    new_type: str | None
    new_outcome: str | None
    merge_into: str | None
    frame_start: int | None
    frame_end: int | None
    note: str = ""


@dataclass(frozen=True)
class TestWindow:
    """一个可排程的试验窗口（窗口-路线-传感器配置组合）。"""

    window_id: str
    region: str
    route_id: str
    visibility_band: str
    permit: str
    sensor_config: str
    capacity: int
    status: str

    @property
    def schedulable(self) -> bool:
        return self.status == WINDOW_OPEN and self.permit != "expired"


@dataclass(frozen=True)
class ClosureEvent:
    """道路封闭或许可变更事件；影响尚未执行的计划。"""

    route_id: str
    region: str
    closed_from: datetime
    reason: str
    permit_after: str | None = None


@dataclass(frozen=True)
class RouteCoverage:
    """路线对能见度分级/场景组合的历史覆盖权重，用于改选损失评估。"""

    route_id: str
    region: str
    visibility_band: str
    target_type: str
    coverage_weight: float


# ---------------------------------------------------------------------------
# 派生记录
# ---------------------------------------------------------------------------


@dataclass
class TripVisibility:
    """行程与能见度观测的匹配结论（含时间错位与部分覆盖痕迹）。"""

    trip_id: str
    band: str
    matched_sensor_id: str | None
    time_gap_seconds: float | None
    observation_quality: str | None
    partial_coverage: bool
    within_gap_limit: bool
    fallback_weather: bool = False
    sources: list[str] = field(default_factory=list)


@dataclass
class Scene:
    """复核更正落库后的唯一障碍场景。

    多个检测帧（或被 merge 的检测）归并为同一场景，``detection_ids``
    保留全部来源，保证“一个障碍多帧出现”不被重复计数。
    """

    scene_id: str
    trip_id: str
    type: str
    frame_start: int
    frame_end: int
    review_state: str
    outcome: str | None
    detection_ids: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)

    @property
    def effective(self) -> bool:
        """是否为真实成立的场景（撤回/合并消失的检测不计）。"""
        return self.review_state != REVIEW_WITHDRAWN

    @property
    def review_mature(self) -> bool:
        """复核是否成熟：confirmed 或 withdrawn 均为终态。"""
        return self.review_state in (REVIEW_CONFIRMED, REVIEW_WITHDRAWN)

    @property
    def definitive_outcome(self) -> bool:
        """是否带有确定性规避结果（unclear 不算）。"""
        return self.outcome in (OUTCOME_AVOIDED, OUTCOME_INTERVENTION)

    @property
    def valid_avoidance_result(self) -> bool:
        """有效规避结果口径：已确认真实场景且结果确定。"""
        return (
            self.review_state == REVIEW_CONFIRMED
            and self.effective
            and self.definitive_outcome
        )


@dataclass
class CoverageCell:
    """版本 × 能见度分级 × 区域的覆盖矩阵单元。"""

    version: str
    band: str
    region: str
    trip_ids: list[str] = field(default_factory=list)
    partial_visibility_trip_ids: list[str] = field(default_factory=list)
    low_upload_trip_ids: list[str] = field(default_factory=list)
    scene_ids: list[str] = field(default_factory=list)
    mature_scene_ids: list[str] = field(default_factory=list)
    water_scene_ids: list[str] = field(default_factory=list)
    valid_avoidance_ids: list[str] = field(default_factory=list)
    avoided_ids: list[str] = field(default_factory=list)
    intervention_ids: list[str] = field(default_factory=list)

    @property
    def trip_count(self) -> int:
        return len(self.trip_ids)

    @property
    def has_evidence(self) -> bool:
        return self.trip_count > 0

    @property
    def valid_avoidance_count(self) -> int:
        return len(self.valid_avoidance_ids)

    @property
    def observed_avoidance_rate(self) -> float | None:
        """观察到的规避成功率；分母为有效规避结果数，不足时为 None。"""
        if not self.valid_avoidance_ids:
            return None
        return len(self.avoided_ids) / len(self.valid_avoidance_ids)


@dataclass
class RetestTarget:
    """由证据缺口推导出的补测目标。"""

    target_id: str
    region: str
    band: str
    target_type: str
    version: str
    gap_kind: str
    reason: str
    required_sensor_config: str
    trip_count: int
    mature_scene_count: int
    valid_avoidance_count: int


@dataclass
class PlanEntry:
    """补测计划条目；reroute 信息与覆盖损失一并留痕。"""

    target_id: str
    region: str
    band: str
    target_type: str
    window_id: str
    route_id: str
    sensor_config: str
    status: str
    gap_kind: str
    primary: bool
    coverage_loss_weight: float | None
    note: str = ""


@dataclass
class Dataset:
    """一次导入的完整资料集合，作为后续各阶段的唯一入口。"""

    visibility: list[VisibilityObservation] = field(default_factory=list)
    weather: list[WeatherObservation] = field(default_factory=list)
    drives: list[Drive] = field(default_factory=list)
    detections: list[ObstacleDetection] = field(default_factory=list)
    review_events: list[ReviewEvent] = field(default_factory=list)
    windows: list[TestWindow] = field(default_factory=list)
    closures: list[ClosureEvent] = field(default_factory=list)
    route_coverage: list[RouteCoverage] = field(default_factory=list)
    source_files: dict[str, str] = field(default_factory=dict)
