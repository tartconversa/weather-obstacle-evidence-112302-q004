"""时间错位匹配测试。

覆盖：观测站与车辆时钟偏差下的就近匹配、missing 质量不可定档、
部分路线覆盖、超错位上限、气象兜底、并列确定性与分级阈值。
"""

from __future__ import annotations

import unittest

from helpers import dt, drive, observation
from low_visibility_obstacle_evidence.models import (
    BAND_CLEAR,
    BAND_LOW,
    BAND_RESTRICTED,
    BAND_UNKNOWN,
    QUALITY_MISSING,
)
from low_visibility_obstacle_evidence.time_match import (
    MatchConfig,
    match_all,
    match_trip_visibility,
    skew_summary,
)


class TimeSkewMatchingTests(unittest.TestCase):
    def test_nearest_observation_used_despite_clock_skew(self):
        # 行程 08:00–09:00，观测分别在 01:00 和 20:00，取更近的 01:00
        trip = drive("T-1", start=dt(8), end=dt(9))
        observations = [
            observation("VS-1", "R-N", dt(1), 2.0),
            observation("VS-2", "R-N", dt(20), 10.0),
        ]
        result = match_trip_visibility(trip, observations)
        self.assertEqual(result.band, BAND_LOW)
        self.assertEqual(result.matched_sensor_id, "VS-1")
        # 行程中点 08:30 与 01:00 相差 7.5 小时
        self.assertAlmostEqual(result.time_gap_seconds, 7.5 * 3600)
        self.assertTrue(result.within_gap_limit)

    def test_missing_quality_cannot_assign_band(self):
        # missing 是“观测缺失”事实，不能被悄悄归入任何分级
        trip = drive("T-1", start=dt(8), end=dt(9))
        observations = [
            observation("VS-1", "R-N", dt(9), 0.5,
                        quality=QUALITY_MISSING, coverage=0.45),
        ]
        result = match_trip_visibility(trip, observations)
        self.assertEqual(result.band, BAND_UNKNOWN)
        self.assertEqual(result.matched_sensor_id, "VS-1")
        self.assertTrue(result.within_gap_limit)
        self.assertIn("missing", result.sources[0])

    def test_observation_beyond_gap_limit_is_unknown(self):
        trip = drive("T-1", start=dt(8, day=10), end=dt(9, day=10))
        observations = [
            observation("VS-1", "R-N", dt(8, day=1), 0.5),
        ]
        config = MatchConfig(max_gap_seconds=6 * 3600)
        result = match_trip_visibility(trip, observations, config=config)
        self.assertEqual(result.band, BAND_UNKNOWN)
        self.assertFalse(result.within_gap_limit)
        self.assertEqual(result.sources, [])

    def test_partial_coverage_flag_propagates(self):
        # coverage_fraction < 1 表示观测只覆盖路线一段
        trip = drive("T-1", start=dt(8), end=dt(9))
        observations = [
            observation("VS-1", "R-N", dt(9), 2.0, coverage=0.45),
        ]
        result = match_trip_visibility(trip, observations)
        self.assertTrue(result.partial_coverage)
        full = match_trip_visibility(
            trip,
            [observation("VS-2", "R-N", dt(9), 2.0, coverage=1.0)],
        )
        self.assertFalse(full.partial_coverage)

    def test_weather_fallback_is_flagged_as_weak_evidence(self):
        from helpers import dataset
        from low_visibility_obstacle_evidence.models import WeatherObservation

        trip = drive("T-1", start=dt(8), end=dt(9))
        # 地面观测超出上限
        ground = [observation("VS-1", "R-N", dt(8, day=1), 0.5)]
        weather = [
            WeatherObservation(
                station_id="WX-9", region="R-N", observed_at=dt(9),
                rain_mm=35.0, visibility_km=0.5, station_coverage=1.0,
            )
        ]
        config = MatchConfig(max_gap_seconds=6 * 3600)
        result = match_trip_visibility(
            trip, ground, weather=weather, config=config
        )
        self.assertEqual(result.band, BAND_RESTRICTED)
        self.assertTrue(result.fallback_weather)
        self.assertIsNone(result.matched_sensor_id)
        self.assertIn("WX-9", result.sources[0])

    def test_missing_does_not_block_a_later_reliable_observation(self):
        # 同一区域有两条观测：missing 更近，validated 稍远但仍在窗口内，
        # 就近匹配先命中 missing 时必须返回 unknown，而不是采用次优观测。
        trip = drive("T-1", start=dt(8), end=dt(9))
        observations = [
            observation("VS-near", "R-N", dt(9), 0.5,
                        quality=QUALITY_MISSING),
            observation("VS-far", "R-N", dt(15), 0.5,
                        quality="validated"),
        ]
        result = match_trip_visibility(trip, observations)
        self.assertEqual(result.band, BAND_UNKNOWN)
        self.assertEqual(result.matched_sensor_id, "VS-near")

    def test_tie_breaks_deterministically_on_sensor_id(self):
        trip = drive("T-1", start=dt(8), end=dt(10))  # 中点 09:00
        observations = [
            observation("VS-Z", "R-N", dt(8), 2.0),
            observation("VS-A", "R-N", dt(10), 2.0),
        ]
        first = match_trip_visibility(trip, observations)
        second = match_trip_visibility(
            trip, list(reversed(observations))
        )
        self.assertEqual(first.matched_sensor_id, second.matched_sensor_id)
        self.assertEqual(first.matched_sensor_id, "VS-Z")

    def test_other_region_observations_ignored(self):
        trip = drive("T-1", region="R-N")
        observations = [
            observation("VS-1", "R-S", dt(8, minute=30), 0.5),
        ]
        result = match_trip_visibility(trip, observations)
        self.assertEqual(result.band, BAND_UNKNOWN)

    def test_band_thresholds(self):
        trip = drive("T-1", start=dt(8), end=dt(9))
        for value, expected in (
            (0.9, BAND_RESTRICTED),
            (1.0, BAND_LOW),
            (4.9, BAND_LOW),
            (5.0, BAND_CLEAR),
        ):
            with self.subTest(value=value):
                result = match_trip_visibility(
                    trip,
                    [observation("VS-1", "R-N", dt(8, minute=30), value)],
                )
                self.assertEqual(result.band, expected)


class BatchAndSummaryTests(unittest.TestCase):
    def test_match_all_and_skew_summary(self):
        trips = [
            drive("T-1", start=dt(8), end=dt(9)),
            drive("T-2", region="R-S", start=dt(8), end=dt(9)),
        ]
        observations = [
            observation("VS-1", "R-N", dt(1), 2.0),
            observation("VS-2", "R-S", dt(8, minute=30), 0.5),
        ]
        matches = match_all(trips, observations)
        self.assertEqual(set(matches), {"T-1", "T-2"})
        summary = skew_summary(matches)
        self.assertEqual(summary["matched"], 2)
        # T-1 错位 7.5h 属于 large_gap；T-2 错位 0
        self.assertEqual(summary["large_gap"], 1)
        self.assertAlmostEqual(summary["max_gap_seconds"], 7.5 * 3600)

    def test_summary_empty_when_no_matches(self):
        summary = skew_summary({})
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(summary["max_gap_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
