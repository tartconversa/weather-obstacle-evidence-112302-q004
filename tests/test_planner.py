"""补测排程与共享窗口争用测试。

覆盖：窗口匹配条件、容量上限下的优先级争用、held/assigned/过期窗口
不可排、改选路线的覆盖损失、无权重时的诚实标注、道路封闭重评估以及
“已执行条目不被后续封闭回改”。
"""

from __future__ import annotations

import unittest

from helpers import closure, dt, route_weight, window
from low_visibility_obstacle_evidence.models import (
    GAP_IMMATURE,
    GAP_NO_EVIDENCE,
    GAP_THIN,
    PLAN_BLOCKED,
    PLAN_EXECUTED,
    PLAN_PROPOSED,
    PLAN_REROUTED,
    RetestTarget,
)
from low_visibility_obstacle_evidence.planner import (
    mark_executed,
    plan_retest,
    reevaluate_plan,
)


def target(target_id: str, *, region="R-N", band="restricted",
           gap=GAP_NO_EVIDENCE, config="CFG-B",
           target_type="water") -> RetestTarget:
    return RetestTarget(
        target_id=target_id, region=region, band=band,
        target_type=target_type, version="V15", gap_kind=gap,
        reason="测试缺口", required_sensor_config=config,
        trip_count=0, mature_scene_count=0, valid_avoidance_count=0,
    )


class WindowEligibilityTests(unittest.TestCase):
    def test_only_open_permitted_windows_are_scheduled(self):
        targets = [target("G-001")]
        windows = [
            window("W-held", "R-N", "RT-3", "restricted", status="held"),
            window("W-assigned", "R-N", "RT-3", "restricted",
                   status="assigned"),
            window("W-expired", "R-N", "RT-3", "restricted",
                   permit="expired"),
            window("W-open", "R-N", "RT-3", "restricted"),
        ]
        plan = plan_retest(targets, windows)
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(plan.entries[0].window_id, "W-open")

    def test_band_region_and_sensor_config_must_all_match(self):
        targets = [target("G-001", band="low", config="CFG-A")]
        windows = [
            window("W-1", "R-N", "RT-3", "restricted",
                   sensor_config="CFG-A"),
            window("W-2", "R-S", "RT-1", "low", sensor_config="CFG-A"),
            window("W-3", "R-N", "RT-3", "low", sensor_config="CFG-B"),
        ]
        plan = plan_retest(targets, windows)
        self.assertEqual(plan.unmet[0].target_id, "G-001")
        self.assertIn("无分级 low", plan.unmet[0].reason)

    def test_closure_excludes_window_when_within_as_of(self):
        targets = [target("G-001")]
        windows = [window("W-1", "R-N", "RT-3", "restricted")]
        closures = [closure("RT-3", "R-N", dt(0, day=20))]
        blocked = plan_retest(targets, windows, closures=closures,
                              as_of=dt(0, day=25))
        self.assertEqual(blocked.unmet[0].target_id, "G-001")
        self.assertIn("封闭", blocked.unmet[0].reason)
        # 评估时点早于封闭日期，窗口仍可用
        usable = plan_retest(targets, windows, closures=closures,
                             as_of=dt(1))
        self.assertEqual(len(usable.entries), 1)


class ContentionTests(unittest.TestCase):
    def test_capacity_contention_prefers_higher_gap_priority(self):
        # 唯一窗口容量 1，三个目标竞争；no_evidence 必须胜出
        targets = [
            target("G-thin", gap=GAP_THIN),
            target("G-imm", gap=GAP_IMMATURE),
            target("G-none", gap=GAP_NO_EVIDENCE),
        ]
        windows = [
            window("W-1", "R-N", "RT-3", "restricted", capacity=1),
        ]
        plan = plan_retest(targets, windows)
        self.assertEqual(len(plan.entries), 1)
        self.assertEqual(plan.entries[0].target_id, "G-none")
        unmet_ids = {item.target_id for item in plan.unmet}
        self.assertEqual(unmet_ids, {"G-thin", "G-imm"})
        # 落选目标必须能看到争用对象，而不是只看到总数变化
        for item in plan.unmet:
            self.assertIn("G-none", item.competing_targets)
            self.assertEqual(item.candidate_window_ids, ["W-1"])

    def test_window_load_tracks_remaining_capacity(self):
        targets = [
            target("G-001"), target("G-002"), target("G-003"),
        ]
        windows = [
            window("W-1", "R-N", "RT-3", "restricted", capacity=2),
        ]
        plan = plan_retest(targets, windows)
        self.assertEqual(plan.window_load["W-1"]["used"], 2)
        self.assertEqual(plan.window_load["W-1"]["capacity"], 2)
        self.assertEqual(len(plan.unmet), 1)

    def test_deterministic_allocation_across_runs(self):
        targets = [
            target("G-001", gap=GAP_NO_EVIDENCE),
            target("G-002", gap=GAP_NO_EVIDENCE),
        ]
        windows = [
            window("W-1", "R-N", "RT-3", "restricted", capacity=1),
            window("W-2", "R-N", "RT-0", "restricted", capacity=1),
        ]
        first = plan_retest(targets, windows)
        second = plan_retest(list(reversed(targets)), windows)
        self.assertEqual(
            [(e.target_id, e.window_id) for e in first.entries],
            [(e.target_id, e.window_id) for e in second.entries],
        )


class RerouteLossTests(unittest.TestCase):
    def test_primary_route_chosen_by_coverage_weight(self):
        targets = [target("G-001")]
        windows = [
            window("W-strong", "R-N", "RT-3", "restricted"),
            window("W-weak", "R-N", "RT-0", "restricted"),
        ]
        coverage = [
            route_weight("RT-3", "R-N", "restricted", 0.85),
            route_weight("RT-0", "R-N", "restricted", 0.45),
        ]
        plan = plan_retest(targets, windows, route_coverage=coverage)
        entry = plan.entries[0]
        self.assertEqual(entry.route_id, "RT-3")
        self.assertTrue(entry.primary)
        self.assertEqual(entry.status, PLAN_PROPOSED)
        self.assertEqual(entry.coverage_loss_weight, 0.0)

    def test_reroute_records_coverage_loss_when_primary_full(self):
        targets = [target("G-001"), target("G-002")]
        windows = [
            window("W-strong", "R-N", "RT-3", "restricted", capacity=1),
            window("W-weak", "R-N", "RT-0", "restricted", capacity=2),
        ]
        coverage = [
            route_weight("RT-3", "R-N", "restricted", 0.85),
            route_weight("RT-0", "R-N", "restricted", 0.45),
        ]
        plan = plan_retest(targets, windows, route_coverage=coverage)
        by_target = {entry.target_id: entry for entry in plan.entries}
        self.assertEqual(by_target["G-001"].route_id, "RT-3")
        rerouted = by_target["G-002"]
        self.assertEqual(rerouted.route_id, "RT-0")
        self.assertFalse(rerouted.primary)
        self.assertEqual(rerouted.status, PLAN_REROUTED)
        self.assertAlmostEqual(rerouted.coverage_loss_weight, 0.40)
        self.assertIn("RT-3", rerouted.note)

    def test_missing_weight_reports_unquantifiable_not_zero(self):
        # 两个窗口容量均为 1；无覆盖权重时首选路线按路线标识确定（RT-0），
        # 第二个目标被迫改选 RT-3，损失必须如实标注为“无法量化”而非 0。
        targets = [target("G-001"), target("G-002")]
        windows = [
            window("W-1", "R-N", "RT-3", "restricted", capacity=1),
            window("W-2", "R-N", "RT-0", "restricted", capacity=1),
        ]
        plan = plan_retest(targets, windows, route_coverage=[])
        rerouted = next(
            entry for entry in plan.entries
            if entry.status == PLAN_REROUTED
        )
        self.assertIsNone(rerouted.coverage_loss_weight)
        self.assertIn("无法量化", rerouted.note)
        # 首选条目不应携带虚假的零损失
        primary = next(
            entry for entry in plan.entries if entry.status == PLAN_PROPOSED
        )
        self.assertIsNone(primary.coverage_loss_weight)


class ReevaluationTests(unittest.TestCase):
    def _plan_with_two_entries(self):
        targets = [target("G-001"), target("G-002")]
        windows = [
            window("W-1", "R-N", "RT-3", "restricted", capacity=2),
        ]
        # 同一窗口容量 2，两个目标都会落到 RT-3
        return plan_retest(targets, windows), windows

    def test_closure_blocks_only_pending_entries(self):
        plan, windows = self._plan_with_two_entries()
        # 先把 G-001 标记为已执行（已采集行程）
        plan = mark_executed(plan, {"G-001"})
        closures = [closure("RT-3", "R-N", dt(0, day=20), "施工封闭")]
        updated = reevaluate_plan(plan, windows, closures, as_of=dt(0, day=25))
        statuses = {entry.target_id: entry.status for entry in updated.entries}
        self.assertEqual(statuses["G-001"], PLAN_EXECUTED)
        self.assertEqual(statuses["G-002"], PLAN_BLOCKED)
        blocked = next(e for e in updated.entries
                       if e.target_id == "G-002")
        self.assertIn("施工封闭", blocked.note)

    def test_permit_expiry_blocks_pending_entry(self):
        plan, windows = self._plan_with_two_entries()
        changed = [
            window("W-1", "R-N", "RT-3", "restricted", permit="expired"),
        ]
        updated = reevaluate_plan(plan, changed, as_of=dt(0, day=25))
        self.assertTrue(
            all(entry.status == PLAN_BLOCKED for entry in updated.entries)
        )
        self.assertIn("许可已过期", updated.entries[0].note)

    def test_reevaluation_before_closure_date_keeps_plan(self):
        plan, windows = self._plan_with_two_entries()
        closures = [closure("RT-3", "R-N", dt(0, day=20))]
        updated = reevaluate_plan(plan, windows, closures, as_of=dt(1))
        self.assertTrue(
            all(entry.status == PLAN_PROPOSED
                for entry in updated.entries)
        )

    def test_executed_entries_survive_later_closure(self):
        plan, windows = self._plan_with_two_entries()
        plan = mark_executed(plan, {"G-001", "G-002"})
        closures = [closure("RT-3", "R-N", dt(0, day=20))]
        updated = reevaluate_plan(plan, windows, closures, as_of=dt(0, day=25))
        self.assertTrue(
            all(entry.status == PLAN_EXECUTED
                for entry in updated.entries)
        )


if __name__ == "__main__":
    unittest.main()
