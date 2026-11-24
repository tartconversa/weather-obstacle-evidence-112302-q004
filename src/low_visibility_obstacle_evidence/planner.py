"""补测计划：窗口匹配、共享窗口争用与改选损失评估。

调度口径：

* 窗口只在 ``status == open`` 且许可未过期时可排程；held/assigned 窗口
  已被既有安排占用，不能再塞入新目标。
* 匹配条件同时约束区域、能见度分级与传感器配置；任一不满足即不可用。
* 同一窗口有容量上限，多个目标竞争同一稀有窗口时，按缺口优先级
  （no_evidence > immature > thin）确定性分配，落选目标进入未满足清单
  并说明争用对象，而不是被静默丢弃。
* 首选路线是该组合历史覆盖权重最高的路线；被迫改选其他路线时，条目带
  ``rerouted`` 状态与覆盖损失权重。权重缺失时损失记为 ``None`` 并在
  备注中明确“无法量化”，绝不把未知损失报成零。
* 道路封闭或许可变更只影响尚未执行的条目；已采集行程仍按当时观测解释，
  由分析层保证，本模块不回改历史结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .models import (
    ClosureEvent,
    GAP_PRIORITY,
    PLAN_BLOCKED,
    PLAN_EXECUTED,
    PLAN_PROPOSED,
    PLAN_REROUTED,
    RetestTarget,
    RouteCoverage,
    TestWindow,
    WINDOW_OPEN,
)


@dataclass
class UnmetTarget:
    """无法排入任何窗口的补测目标及原因。"""

    target_id: str
    region: str
    band: str
    target_type: str
    gap_kind: str
    reason: str
    competing_targets: list[str] = field(default_factory=list)
    candidate_window_ids: list[str] = field(default_factory=list)


@dataclass
class RetestPlan:
    """一轮排程的完整结果。"""

    entries: list = field(default_factory=list)          # PlanEntry
    unmet: list[UnmetTarget] = field(default_factory=list)
    window_load: dict[str, dict[str, object]] = field(default_factory=dict)
    closed_routes: list[str] = field(default_factory=list)
    as_of: datetime | None = None

    @property
    def pending_entries(self):
        return [
            entry for entry in self.entries
            if entry.status not in (PLAN_EXECUTED, PLAN_BLOCKED)
        ]


def _active_closures(closures: list[ClosureEvent],
                     as_of: datetime | None) -> dict[str, ClosureEvent]:
    if as_of is None:
        return {closure.route_id: closure for closure in closures}
    return {
        closure.route_id: closure
        for closure in closures
        if closure.closed_from <= as_of
    }


def _eligible_windows(target: RetestTarget, windows: list[TestWindow],
                      closed_routes: set[str]) -> list[TestWindow]:
    return [
        window for window in windows
        if window.region == target.region
        and window.visibility_band == target.band
        and window.sensor_config == target.required_sensor_config
        and window.schedulable
        and window.status == WINDOW_OPEN
        and window.route_id not in closed_routes
    ]


def _coverage_lookup(route_coverage: list[RouteCoverage]) -> dict:
    lookup: dict[tuple[str, str, str], float] = {}
    for item in route_coverage:
        lookup[(item.route_id, item.visibility_band, item.target_type)] = \
            item.coverage_weight
    return lookup


def plan_retest(
    targets: list[RetestTarget],
    windows: list[TestWindow],
    route_coverage: list[RouteCoverage] | None = None,
    closures: list[ClosureEvent] | None = None,
    as_of: datetime | None = None,
) -> RetestPlan:
    """按缺口优先级把补测目标分配到合规窗口。"""
    closures = closures or []
    active = _active_closures(closures, as_of)
    closed_routes = set(active)
    weights = _coverage_lookup(route_coverage or [])

    load: dict[str, int] = {window.window_id: 0 for window in windows}
    entries = []
    unmet: list[UnmetTarget] = []

    ordered = sorted(
        targets,
        key=lambda t: (-GAP_PRIORITY[t.gap_kind], t.region, t.band,
                       t.target_id),
    )
    # 记录每个窗口的目标队列，用于落选目标说明争用对象。
    queues: dict[str, list[str]] = {window.window_id: []
                                    for window in windows}

    for target in ordered:
        candidates = _eligible_windows(target, windows, closed_routes)
        for window in candidates:
            queues[window.window_id].append(target.target_id)

        if not candidates:
            unmet.append(UnmetTarget(
                target_id=target.target_id, region=target.region,
                band=target.band, target_type=target.target_type,
                gap_kind=target.gap_kind,
                reason=_no_window_reason(target, windows, closed_routes),
                candidate_window_ids=[],
            ))
            continue

        primary_route = _primary_route(target, candidates, weights)
        chosen = _choose_window(target, candidates, load, weights,
                                primary_route)
        if chosen is None:
            unmet.append(UnmetTarget(
                target_id=target.target_id, region=target.region,
                band=target.band, target_type=target.target_type,
                gap_kind=target.gap_kind,
                reason="合规窗口容量均已被更高优先级目标占满（共享窗口争用）",
                competing_targets=sorted({
                    tid for window in candidates
                    for tid in queues[window.window_id]
                    if tid != target.target_id
                }),
                candidate_window_ids=[w.window_id for w in candidates],
            ))
            continue

        load[chosen.window_id] += 1
        is_primary = chosen.route_id == primary_route
        loss = _coverage_loss(target, chosen.route_id, primary_route, weights)
        note = ""
        if not is_primary:
            note = (
                f"首选路线 {primary_route} 容量不足，改选 {chosen.route_id}"
            )
            if loss is None:
                note += "；路线覆盖权重缺失，覆盖损失无法量化"
        entries.append(_entry(target, chosen, is_primary, loss, note))

    return RetestPlan(
        entries=entries, unmet=unmet,
        window_load={
            window_id: {
                "used": used,
                "capacity": next(
                    w.capacity for w in windows if w.window_id == window_id
                ),
                "route_id": next(
                    w.route_id for w in windows if w.window_id == window_id
                ),
            }
            for window_id, used in load.items()
        },
        closed_routes=sorted(closed_routes),
        as_of=as_of,
    )


def _no_window_reason(target: RetestTarget,
                      windows: list[TestWindow],
                      closed_routes: set[str]) -> str:
    same_region = [w for w in windows if w.region == target.region]
    if not same_region:
        return f"区域 {target.region} 没有任何登记窗口"
    matching = [
        w for w in same_region
        if w.visibility_band == target.band
        and w.sensor_config == target.required_sensor_config
    ]
    if not matching:
        return (
            f"区域 {target.region} 无分级 {target.band} 且传感器配置 "
            f"{target.required_sensor_config} 的窗口"
        )
    blocked = [
        w for w in matching
        if not w.schedulable or w.route_id in closed_routes
    ]
    if blocked and len(blocked) == len(matching):
        details = ", ".join(
            f"{w.window_id}({_block_reason(w, closed_routes)})"
            for w in matching
        )
        return f"匹配窗口均不可用：{details}"
    return "无合规窗口"


def _block_reason(window: TestWindow, closed_routes: set[str]) -> str:
    if window.route_id in closed_routes:
        return "道路封闭"
    if window.permit == "expired":
        return "许可过期"
    return f"状态 {window.status}"


def _primary_route(target: RetestTarget,
                   candidates: list[TestWindow],
                   weights: dict) -> str:
    route_ids = sorted({window.route_id for window in candidates})

    def weight(route_id: str):
        return weights.get(
            (route_id, target.band, target.target_type)
        )

    known = [(route_id, weight(route_id)) for route_id in route_ids]
    weighted = [item for item in known if item[1] is not None]
    if weighted:
        weighted.sort(key=lambda item: (-item[1], item[0]))
        return weighted[0][0]
    # 无权重资料时按路线标识确定性选择，并在损失备注中如实说明。
    return route_ids[0]


def _choose_window(target: RetestTarget,
                   candidates: list[TestWindow],
                   load: dict[str, int],
                   weights: dict,
                   primary_route: str) -> TestWindow | None:
    available = [w for w in candidates if load[w.window_id] < w.capacity]
    if not available:
        return None
    # 先尝试首选路线，保持容量与覆盖的权衡可解释。
    primary = [w for w in available if w.route_id == primary_route]
    pool = primary or available
    pool.sort(key=lambda w: (
        0 if w.route_id == primary_route else 1,
        -_safe_weight(weights, target, w.route_id),
        w.window_id,
    ))
    return pool[0]


def _safe_weight(weights: dict, target: RetestTarget, route_id: str) -> float:
    value = weights.get((route_id, target.band, target.target_type))
    return value if value is not None else 0.0


def _coverage_loss(target: RetestTarget, chosen_route: str,
                   primary_route: str, weights: dict) -> float | None:
    if chosen_route == primary_route:
        primary_weight = weights.get(
            (primary_route, target.band, target.target_type)
        )
        return 0.0 if primary_weight is not None else None
    primary_weight = weights.get(
        (primary_route, target.band, target.target_type)
    )
    chosen_weight = weights.get(
        (chosen_route, target.band, target.target_type)
    )
    if primary_weight is None or chosen_weight is None:
        return None
    return max(0.0, primary_weight - chosen_weight)


def _entry(target: RetestTarget, window: TestWindow,
           is_primary: bool, loss: float | None, note: str):
    from .models import PlanEntry
    return PlanEntry(
        target_id=target.target_id, region=target.region,
        band=target.band, target_type=target.target_type,
        window_id=window.window_id, route_id=window.route_id,
        sensor_config=window.sensor_config,
        status=PLAN_PROPOSED if is_primary else PLAN_REROUTED,
        gap_kind=target.gap_kind, primary=is_primary,
        coverage_loss_weight=loss,
        note=note,
    )


def reevaluate_plan(
    plan: RetestPlan,
    windows: list[TestWindow],
    closures: list[ClosureEvent] | None = None,
    as_of: datetime | None = None,
) -> RetestPlan:
    """道路封闭或许可变化后重新评估尚未执行的计划条目。

    已执行（``executed``）条目不回改；其余条目若路线已封闭、窗口许可
    过期或窗口不再开放，标记为 ``blocked`` 并附原因，等待重新排程。
    """
    closures = closures or []
    active = _active_closures(closures, as_of)
    window_by_id = {window.window_id: window for window in windows}

    new_entries = []
    blocked_notes: list[str] = []
    for entry in plan.entries:
        if entry.status == PLAN_EXECUTED:
            new_entries.append(entry)
            continue
        reasons: list[str] = []
        if entry.route_id in active:
            reasons.append(
                f"路线 {entry.route_id} 封闭：{active[entry.route_id].reason}"
            )
        window = window_by_id.get(entry.window_id)
        if window is not None:
            if window.permit == "expired":
                reasons.append(f"窗口 {window.window_id} 许可已过期")
            if window.status != WINDOW_OPEN:
                reasons.append(
                    f"窗口 {window.window_id} 状态变为 {window.status}"
                )
        if reasons:
            blocked = _blocked_copy(entry, "；".join(reasons))
            new_entries.append(blocked)
            blocked_notes.append(blocked.note)
        else:
            new_entries.append(entry)

    return RetestPlan(
        entries=new_entries, unmet=list(plan.unmet),
        window_load=dict(plan.window_load),
        closed_routes=sorted(set(plan.closed_routes) | set(active)),
        as_of=as_of,
    )


def _blocked_copy(entry, note: str):
    from .models import PlanEntry
    return PlanEntry(
        target_id=entry.target_id, region=entry.region, band=entry.band,
        target_type=entry.target_type, window_id=entry.window_id,
        route_id=entry.route_id, sensor_config=entry.sensor_config,
        status=PLAN_BLOCKED, gap_kind=entry.gap_kind,
        primary=entry.primary,
        coverage_loss_weight=entry.coverage_loss_weight, note=note,
    )


def mark_executed(plan: RetestPlan, target_ids: set[str]) -> RetestPlan:
    """把已完成采集的条目标记为 executed；历史结论不受后续封闭影响。"""
    updated = []
    for entry in plan.entries:
        if entry.target_id in target_ids and entry.status != PLAN_BLOCKED:
            from .models import PlanEntry
            updated.append(PlanEntry(
                target_id=entry.target_id, region=entry.region,
                band=entry.band, target_type=entry.target_type,
                window_id=entry.window_id, route_id=entry.route_id,
                sensor_config=entry.sensor_config, status=PLAN_EXECUTED,
                gap_kind=entry.gap_kind, primary=entry.primary,
                coverage_loss_weight=entry.coverage_loss_weight,
                note=entry.note,
            ))
        else:
            updated.append(entry)
    return RetestPlan(
        entries=updated, unmet=list(plan.unmet),
        window_load=dict(plan.window_load),
        closed_routes=list(plan.closed_routes), as_of=plan.as_of,
    )
