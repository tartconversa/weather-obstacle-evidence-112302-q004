"""端到端分析测试：三口径分离、覆盖矩阵、版本对比与缺口识别。"""

from __future__ import annotations

import unittest

from helpers import dataset, detection, drive, dt, observation
from low_visibility_obstacle_evidence.analysis import (
    MIN_VALID_RESULTS,
    build_analysis,
)
from low_visibility_obstacle_evidence.models import (
    BAND_LOW,
    BAND_RESTRICTED,
    GAP_IMMATURE,
    GAP_NO_EVIDENCE,
    GAP_THIN,
    OUTCOME_AVOIDED,
    OUTCOME_INTERVENTION,
    OUTCOME_UNCLEAR,
    REVIEW_CONFIRMED,
    REVIEW_PENDING,
    ReviewEvent,
)


def review(event_id, obstacle_id, action, *, new_type=None,
           new_outcome=None):
    return ReviewEvent(
        event_id=event_id, obstacle_id=obstacle_id,
        reviewed_at=dt(12), action=action, new_type=new_type,
        new_outcome=new_outcome, merge_into=None,
        frame_start=None, frame_end=None, note="",
    )


class CaliberSeparationTests(unittest.TestCase):
    def _build(self):
        # 两条 V15 行程均在 R-N、低能见度；一条含已确认积水并成功规避，
        # 另一条的检测待复核（不计入有效规避结果）。
        drives = [
            drive("T-1", version="V15", start=dt(8), end=dt(9)),
            drive("T-2", version="V15", start=dt(8), end=dt(9)),
        ]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 2.0),
        ]
        detections = [
            detection("O-1", "T-1", 10, 14, review_state="confirmed",
                      outcome=OUTCOME_AVOIDED),
            # 待复核的散落物：不是积水，也不进入有效规避结果
            detection("O-2", "T-2", 10, 14, type="debris",
                      review_state=REVIEW_PENDING,
                      outcome=OUTCOME_INTERVENTION),
        ]
        events = [
            review("RV-1", "O-1", "confirm", new_type="water",
                   new_outcome=OUTCOME_AVOIDED),
        ]
        return build_analysis(dataset(
            drives=drives, visibility=visibility,
            detections=detections, review_events=events,
        ))

    def test_three_calibers_are_not_interchangeable(self):
        analysis = self._build()
        caliber = analysis.caliber["V15"]
        self.assertEqual(caliber["low_visibility_trips"], 2)
        self.assertEqual(caliber["water_scenes"], 1)
        self.assertEqual(caliber["valid_avoidance_results"], 1)
        self.assertEqual(caliber["avoided"], 1)
        self.assertEqual(caliber["intervention"], 0)

    def test_pending_detection_not_in_valid_results(self):
        analysis = self._build()
        cell = analysis.cells[("V15", BAND_LOW, "R-N")]
        self.assertEqual(cell.scene_ids, ["S-O-1", "S-O-2"])
        self.assertEqual(cell.valid_avoidance_count, 1)
        self.assertAlmostEqual(cell.observed_avoidance_rate, 1.0)


class NoEvidenceTests(unittest.TestCase):
    def test_uncovered_combination_is_no_evidence_not_zero_rate(self):
        # R-S 区域有观测但车队从未行驶；R-N 有行程但无积水场景
        drives = [drive("T-1", region="R-N", version="V15")]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 0.5),
            observation("VS-2", "R-S", dt(8, minute=30), 0.5),
        ]
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
        ))
        # R-S 任何分级都没有单元 → 无证据
        self.assertNotIn(("V15", BAND_RESTRICTED, "R-S"),
                         analysis.cells)
        cell_n = analysis.cells[("V15", BAND_RESTRICTED, "R-N")]
        self.assertTrue(cell_n.has_evidence)
        self.assertIsNone(cell_n.observed_avoidance_rate)
        # 补测目标必须覆盖两个区域的 restricted/low 缺口
        target_keys = {(t.region, t.band) for t in analysis.targets}
        self.assertIn(("R-S", BAND_RESTRICTED), target_keys)
        self.assertIn(("R-S", BAND_LOW), target_keys)
        r_n_restricted = next(
            t for t in analysis.targets
            if t.region == "R-N" and t.band == BAND_RESTRICTED
        )
        self.assertEqual(r_n_restricted.gap_kind, GAP_NO_EVIDENCE)
        self.assertIn("未观测到任何积水场景", r_n_restricted.reason)

    def test_unknown_band_trips_excluded_from_all_cells(self):
        drives = [drive("T-1", version="V15")]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 0.5,
                        quality="missing"),
        ]
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
        ))
        self.assertEqual(len(analysis.cells), 0)
        self.assertEqual(len(analysis.unknown_trips), 1)
        self.assertIn("missing", analysis.unknown_trips[0].reason)
        self.assertEqual(
            analysis.caliber["V15"]["low_visibility_trips"], 0
        )
        self.assertEqual(
            analysis.caliber["V15"]["unknown_band_trips"], 1
        )


class MaturityAndGapTests(unittest.TestCase):
    def test_immature_water_scene_generates_immature_target(self):
        drives = [drive("T-1", version="V15")]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 0.5),
        ]
        detections = [
            detection("O-1", "T-1", 10, 14, review_state=REVIEW_PENDING,
                      outcome=OUTCOME_AVOIDED),
        ]
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
            detections=detections,
        ))
        target = next(
            t for t in analysis.targets
            if t.region == "R-N" and t.band == BAND_RESTRICTED
        )
        self.assertEqual(target.gap_kind, GAP_IMMATURE)
        self.assertEqual(target.mature_scene_count, 0)

    def test_thin_gap_when_valid_results_below_threshold(self):
        drives = [drive(f"T-{i}", version="V15")
                  for i in range(1, MIN_VALID_RESULTS + 1)]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 0.5),
        ]
        # 只有 1 个有效结果，低于 MIN_VALID_RESULTS
        detections = [
            detection("O-1", "T-1", 10, 14, review_state="confirmed",
                      outcome=OUTCOME_AVOIDED),
        ]
        events = [review("RV-1", "O-1", "confirm", new_type="water",
                         new_outcome=OUTCOME_AVOIDED)]
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
            detections=detections, review_events=events,
        ))
        target = next(
            t for t in analysis.targets
            if t.region == "R-N" and t.band == BAND_RESTRICTED
        )
        self.assertEqual(target.gap_kind, GAP_THIN)
        self.assertEqual(target.valid_avoidance_count, 1)


class VersionComparisonTests(unittest.TestCase):
    def test_extrapolatable_only_when_both_sides_have_enough_results(self):
        drives = []
        for i in range(1, MIN_VALID_RESULTS + 1):
            drives.append(drive(f"TA-{i}", version="V14"))
            drives.append(drive(f"TB-{i}", version="V15"))
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 0.5),
        ]
        detections = []
        events = []
        for i in range(1, MIN_VALID_RESULTS + 1):
            detections.append(detection(
                f"OA-{i}", f"TA-{i}", 10, 14,
                review_state="confirmed", outcome=OUTCOME_AVOIDED,
            ))
            detections.append(detection(
                f"OB-{i}", f"TB-{i}", 10, 14,
                review_state="confirmed",
                outcome=(OUTCOME_INTERVENTION if i == 1
                         else OUTCOME_AVOIDED),
            ))
            events.append(review(f"RVA-{i}", f"OA-{i}", "confirm",
                                 new_type="water",
                                 new_outcome=OUTCOME_AVOIDED))
            events.append(review(
                f"RVB-{i}", f"OB-{i}", "confirm", new_type="water",
                new_outcome=(OUTCOME_INTERVENTION if i == 1
                             else OUTCOME_AVOIDED),
            ))
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
            detections=detections, review_events=events,
        ))
        entry = next(
            c for c in analysis.comparisons
            if c.band == BAND_RESTRICTED and c.region == "R-N"
        )
        self.assertTrue(entry.extrapolatable)
        # V14 100%，V15 (MIN-1)/MIN
        self.assertAlmostEqual(entry.rates[0], 1.0)
        self.assertAlmostEqual(entry.rates[1],
                               (MIN_VALID_RESULTS - 1) / MIN_VALID_RESULTS)
        self.assertAlmostEqual(entry.rate_difference, -1 / MIN_VALID_RESULTS)

    def test_comparison_nonextrapolatable_when_one_side_has_no_evidence(self):
        # V14 在 R-N restricted 有行程；V15 只在 R-S 行驶，
        # R-N×restricted 的 V15 侧因此零行程、无证据
        drives = [
            drive("T-1", region="R-N", version="V14"),
            drive("T-2", region="R-S", version="V15"),
        ]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 0.5),
            observation("VS-2", "R-S", dt(8, minute=30), 0.5),
        ]
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
        ))
        entry = next(
            c for c in analysis.comparisons
            if c.band == BAND_RESTRICTED and c.region == "R-N"
        )
        self.assertFalse(entry.extrapolatable)
        self.assertIsNone(entry.rate_difference)
        self.assertTrue(
            any("V15" in reason for reason in entry.reasons)
        )

    def test_upload_gap_recorded_as_comparison_caveat(self):
        drives = [
            drive("T-1", version="V14", upload_fraction=1.0),
            drive("T-2", version="V15", upload_fraction=0.35),
        ]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 0.5),
        ]
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
        ))
        entry = next(
            c for c in analysis.comparisons
            if c.band == BAND_RESTRICTED and c.region == "R-N"
        )
        self.assertTrue(
            any("上传不完整" in reason for reason in entry.reasons)
        )

    def test_unclear_outcome_does_not_enter_rate_denominator(self):
        drives = [drive("T-1", version="V15")]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 0.5),
        ]
        detections = [
            detection("O-1", "T-1", 10, 14, review_state="confirmed",
                      outcome=OUTCOME_UNCLEAR),
        ]
        events = [review("RV-1", "O-1", "confirm", new_type="water",
                         new_outcome=OUTCOME_UNCLEAR)]
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
            detections=detections, review_events=events,
        ))
        cell = analysis.cells[("V15", BAND_RESTRICTED, "R-N")]
        self.assertEqual(len(cell.water_scene_ids), 1)
        self.assertEqual(cell.valid_avoidance_count, 0)
        self.assertIsNone(cell.observed_avoidance_rate)


class PartialCoverageTests(unittest.TestCase):
    def test_partial_visibility_marked_in_cell(self):
        drives = [
            drive("T-1", version="V14"),
            drive("T-2", version="V15"),
        ]
        visibility = [
            observation("VS-1", "R-N", dt(8, minute=30), 2.0,
                        coverage=0.45),
        ]
        analysis = build_analysis(dataset(
            drives=drives, visibility=visibility,
        ))
        cell = analysis.cells[("V15", BAND_LOW, "R-N")]
        self.assertEqual(cell.partial_visibility_trip_ids, ["T-2"])
        comparison = next(
            c for c in analysis.comparisons
            if c.band == BAND_LOW and c.region == "R-N"
        )
        self.assertTrue(
            any("部分路线" in reason for reason in comparison.reasons)
        )


class WeatherFallbackIntegrationTests(unittest.TestCase):
    def test_weather_fallback_trip_enters_matrix_with_weak_flag(self):
        from helpers import dataset as make_dataset
        from low_visibility_obstacle_evidence.models import (
            WeatherObservation,
        )

        # 地面观测与行程错位超过上限，气象站在行程中点附近可兜底
        drives = [drive("T-1", version="V15")]
        ground = [
            observation("VS-1", "R-N", dt(8, day=1), 0.5),
        ]
        weather = [
            WeatherObservation(
                station_id="WX-9", region="R-N", observed_at=dt(8, minute=30),
                rain_mm=35.0, visibility_km=0.5, station_coverage=1.0,
            )
        ]
        analysis = build_analysis(make_dataset(
            drives=drives, visibility=ground, weather=weather,
        ))
        match = analysis.matches["T-1"]
        self.assertTrue(match.fallback_weather)
        self.assertEqual(match.band, BAND_RESTRICTED)
        # 兜底定档仍计入矩阵，但来源可追溯为气象站
        self.assertIn(
            ("V15", BAND_RESTRICTED, "R-N"), analysis.cells
        )


class CoverageLossAggregationTests(unittest.TestCase):
    def test_reroute_keeps_target_count_but_surfaces_loss(self):
        from helpers import route_weight, window
        from low_visibility_obstacle_evidence.planner import plan_retest
        from low_visibility_obstacle_evidence.models import (
            GAP_NO_EVIDENCE,
            RetestTarget,
        )

        def make_target(tid):
            return RetestTarget(
                target_id=tid, region="R-N", band=BAND_RESTRICTED,
                target_type="water", version="V15",
                gap_kind=GAP_NO_EVIDENCE, reason="无证据",
                required_sensor_config="CFG-B", trip_count=0,
                mature_scene_count=0, valid_avoidance_count=0,
            )

        targets = [make_target("G-001"), make_target("G-002")]
        windows = [
            window("W-strong", "R-N", "RT-3", BAND_RESTRICTED,
                   capacity=1),
            window("W-weak", "R-N", "RT-0", BAND_RESTRICTED,
                   capacity=1),
        ]
        coverage = [
            route_weight("RT-3", "R-N", BAND_RESTRICTED, 0.85),
            route_weight("RT-0", "R-N", BAND_RESTRICTED, 0.45),
        ]
        plan = plan_retest(targets, windows, route_coverage=coverage)
        # 两个目标都被排程：总数没有因为改选而虚增或丢失
        self.assertEqual(len(plan.entries), 2)
        self.assertEqual(plan.unmet, [])
        losses = {
            entry.target_id: entry.coverage_loss_weight
            for entry in plan.entries
        }
        self.assertEqual(losses["G-001"], 0.0)
        self.assertAlmostEqual(losses["G-002"], 0.40)


if __name__ == "__main__":
    unittest.main()
