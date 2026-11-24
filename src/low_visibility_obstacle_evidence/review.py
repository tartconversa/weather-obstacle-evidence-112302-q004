"""人工复核更正的事件落库与场景重建。

原始 ``obstacles.csv`` 是传感器侧检测，一个障碍可能在多个帧里重复出现；
人工复核以事件流（``review_events.csv``）给出撤回、改判、合并与边界
拆分。分析必须以“更正后的场景”为准，而不是直接统计原始检测。

处理规则：

1. 事件按 ``reviewed_at``（并列按 ``event_id``）顺序施加，保证更正
   可重放、可审计；每条事件编号都会留存在场景的 ``corrections`` 中。
2. ``reject`` 撤回误检，场景不再成立；``merge`` 把检测并入目标检测，
   帧区间取并集，来源检测不单独计数。
3. ``split`` 收窄帧边界，被拆出的部分不再属于原检测；只有帧区间真正
   重叠的检测才归并为同一场景，相邻不重叠即两个场景。
4. 场景成熟度以复核终态为准：``confirmed`` 才成熟；``pending`` 与
   ``revised`` 都属于复核未成熟，其结果不能进入有效规避结果口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    ObstacleDetection,
    OUTCOME_UNCLEAR,
    REVIEW_CONFIRMED,
    REVIEW_PENDING,
    REVIEW_REVISED,
    REVIEW_WITHDRAWN,
    ReviewEvent,
    Scene,
)


@dataclass
class _DetectionState:
    """事件施加过程中的可变检测状态。"""

    obstacle_id: str
    trip_id: str
    frame_start: int
    frame_end: int
    type: str
    outcome: str | None
    review_state: str
    status: str = "active"  # active / withdrawn / merged
    merged_into: str | None = None
    corrections: list[str] = field(default_factory=list)


def _apply_event(state: _DetectionState, event: ReviewEvent,
                 index: dict[str, _DetectionState]) -> None:
    state.corrections.append(event.event_id)

    if event.action == "reject":
        state.status = "withdrawn"
        state.review_state = REVIEW_WITHDRAWN
        state.outcome = None
        return

    if event.action == "confirm":
        if event.new_type is not None:
            state.type = event.new_type
        if event.new_outcome is not None:
            state.outcome = event.new_outcome
        state.review_state = REVIEW_CONFIRMED
        return

    if event.action == "reclassify":
        if event.new_type is not None:
            state.type = event.new_type
        if event.new_outcome is not None:
            state.outcome = event.new_outcome
        # 改判意味着人工已对场景定性，进入确认终态。
        state.review_state = REVIEW_CONFIRMED
        return

    if event.action == "split":
        if event.frame_start is None or event.frame_end is None:
            raise ValueError(
                f"复核事件 {event.event_id} 为 split 但缺少新的帧边界"
            )
        if not (state.frame_start <= event.frame_start
                <= event.frame_end <= state.frame_end):
            raise ValueError(
                f"复核事件 {event.event_id} 的拆分边界 "
                f"[{event.frame_start},{event.frame_end}] 超出原检测 "
                f"{state.obstacle_id} 的帧区间 "
                f"[{state.frame_start},{state.frame_end}]"
            )
        state.frame_start = event.frame_start
        state.frame_end = event.frame_end
        if state.review_state == REVIEW_PENDING:
            state.review_state = REVIEW_REVISED
        return

    if event.action == "merge":
        if event.merge_into is None:
            raise ValueError(
                f"复核事件 {event.event_id} 为 merge 但缺少 merge_into"
            )
        target = index.get(event.merge_into)
        if target is None:
            raise ValueError(
                f"复核事件 {event.event_id} 合并目标 {event.merge_into} 不存在"
            )
        if target.status != "active":
            raise ValueError(
                f"复核事件 {event.event_id} 不能并入状态为 "
                f"{target.status} 的检测 {target.obstacle_id}"
            )
        state.status = "merged"
        state.merged_into = target.obstacle_id
        target.frame_start = min(target.frame_start, state.frame_start)
        target.frame_end = max(target.frame_end, state.frame_end)
        target.corrections.append(event.event_id)
        return

    raise ValueError(f"未知复核动作: {event.action}")  # pragma: no cover


def apply_review(
    detections: list[ObstacleDetection],
    review_events: list[ReviewEvent] | None = None,
) -> list[Scene]:
    """按事件流重放复核，返回去重后的唯一场景列表。"""
    index: dict[str, _DetectionState] = {
        d.obstacle_id: _DetectionState(
            obstacle_id=d.obstacle_id, trip_id=d.trip_id,
            frame_start=d.frame_start, frame_end=d.frame_end,
            type=d.type, outcome=d.outcome, review_state=d.review_state,
        )
        for d in detections
    }

    ordered = sorted(
        review_events or [], key=lambda e: (e.reviewed_at, e.event_id)
    )
    for event in ordered:
        state = index.get(event.obstacle_id)
        if state is None:  # 导入层已做外键校验，这里仅防御
            raise ValueError(
                f"复核事件 {event.event_id} 指向未知检测 {event.obstacle_id}"
            )
        if state.status in ("withdrawn", "merged"):
            raise ValueError(
                f"复核事件 {event.event_id} 作用于已终结的检测 "
                f"{state.obstacle_id}（{state.status}）"
            )
        _apply_event(state, event, index)

    # 合并链最终校验：被合并检测的目标必须仍有效。
    for state in index.values():
        if state.status == "merged":
            target = index[state.merged_into]  # type: ignore[index]
            if target.status != "active":
                raise ValueError(
                    f"检测 {state.obstacle_id} 的合并目标 "
                    f"{target.obstacle_id} 最终状态为 {target.status}"
                )

    active = [s for s in index.values() if s.status == "active"]
    withdrawn = [s for s in index.values() if s.status == "withdrawn"]
    scenes = _group_into_scenes(active)
    # 撤回检测不与任何真实场景归并，单独保留为 effective=False 的场景，
    # 便于报告追溯“误检撤回”这一业务事实；统计层会跳过它们。
    for state in sorted(withdrawn, key=lambda s: s.obstacle_id):
        scenes.append(Scene(
            scene_id=f"S-{state.obstacle_id}", trip_id=state.trip_id,
            type=state.type, frame_start=state.frame_start,
            frame_end=state.frame_end, review_state=REVIEW_WITHDRAWN,
            outcome=None, detection_ids=[state.obstacle_id],
            corrections=list(state.corrections),
        ))
    return sorted(scenes, key=lambda scene: scene.scene_id)


def _group_into_scenes(active: list[_DetectionState]) -> list[Scene]:
    """同行程内帧区间重叠的检测归并为同一场景。"""
    by_trip: dict[str, list[_DetectionState]] = {}
    for state in active:
        by_trip.setdefault(state.trip_id, []).append(state)

    scenes: list[Scene] = []
    for trip_id in sorted(by_trip):
        states = sorted(by_trip[trip_id],
                        key=lambda s: (s.frame_start, s.obstacle_id))
        groups: list[list[_DetectionState]] = []
        for state in states:
            if groups and state.frame_start <= groups[-1][-1].frame_end:
                groups[-1].append(state)
            else:
                groups.append([state])

        for group in groups:
            scenes.append(_build_scene(trip_id, group))
    return scenes


def _build_scene(trip_id: str, group: list[_DetectionState]) -> Scene:
    frame_start = min(s.frame_start for s in group)
    frame_end = max(s.frame_end for s in group)
    # 代表性检测：优先已确认者，其次标识最小者，保证确定性。
    group_sorted = sorted(
        group,
        key=lambda s: (s.review_state != REVIEW_CONFIRMED, s.obstacle_id),
    )
    representative = group_sorted[0]

    if any(s.review_state == REVIEW_CONFIRMED for s in group):
        review_state = REVIEW_CONFIRMED
    elif any(s.review_state == REVIEW_REVISED for s in group):
        review_state = REVIEW_REVISED
    else:
        review_state = REVIEW_PENDING

    # 结果字段保留原始/更正值；是否可作为“有效规避结果”由
    # Scene.valid_avoidance_result 统一把关（必须 confirmed 且结果确定），
    # 未成熟场景的结果不会进入任何事件率分母。
    outcome = representative.outcome

    corrections = sorted({
        correction for s in group for correction in s.corrections
    })
    detection_ids = sorted(s.obstacle_id for s in group)
    return Scene(
        scene_id=f"S-{representative.obstacle_id}",
        trip_id=trip_id, type=representative.type,
        frame_start=frame_start, frame_end=frame_end,
        review_state=review_state, outcome=outcome,
        detection_ids=detection_ids, corrections=corrections,
    )
