"""人工复核更正测试。

覆盖：多帧/多检测去重、撤回误检、合并同一障碍、边界拆分、类型改判、
结果更正、事件顺序约束，以及“有效规避结果”口径对成熟度的要求。
"""

from __future__ import annotations

import unittest

from helpers import detection, dt
from low_visibility_obstacle_evidence.models import (
    OUTCOME_AVOIDED,
    OUTCOME_INTERVENTION,
    OUTCOME_UNCLEAR,
    REVIEW_CONFIRMED,
    REVIEW_PENDING,
    REVIEW_REVISED,
    REVIEW_WITHDRAWN,
    ReviewEvent,
)
from low_visibility_obstacle_evidence.review import apply_review


def event(event_id: str, obstacle_id: str, action: str, *,
          reviewed_at=None, new_type=None, new_outcome=None,
          merge_into=None, frame_start=None, frame_end=None) -> ReviewEvent:
    return ReviewEvent(
        event_id=event_id, obstacle_id=obstacle_id,
        reviewed_at=reviewed_at or dt(12), action=action,
        new_type=new_type, new_outcome=new_outcome, merge_into=merge_into,
        frame_start=frame_start, frame_end=frame_end, note="",
    )


class DeduplicationTests(unittest.TestCase):
    def test_overlapping_frames_merge_into_one_scene(self):
        detections = [
            detection("O-1", "T-1", 10, 14),
            detection("O-2", "T-1", 13, 18),
        ]
        scenes = apply_review(detections, [])
        self.assertEqual(len(scenes), 1)
        scene = scenes[0]
        self.assertEqual(scene.frame_start, 10)
        self.assertEqual(scene.frame_end, 18)
        self.assertEqual(scene.detection_ids, ["O-1", "O-2"])

    def test_adjacent_but_non_overlapping_frames_are_separate(self):
        detections = [
            detection("O-1", "T-1", 10, 14),
            detection("O-2", "T-1", 15, 20),
        ]
        scenes = apply_review(detections, [])
        self.assertEqual(len(scenes), 2)

    def test_detections_on_different_trips_never_merge(self):
        detections = [
            detection("O-1", "T-1", 10, 14),
            detection("O-2", "T-2", 12, 16),
        ]
        scenes = apply_review(detections, [])
        self.assertEqual(len(scenes), 2)
        self.assertEqual({s.trip_id for s in scenes}, {"T-1", "T-2"})


class ReviewCorrectionTests(unittest.TestCase):
    def test_reject_withdraws_detection_and_drops_scene(self):
        detections = [detection("O-1", "T-1", 10, 14)]
        events = [event("RV-1", "O-1", "reject")]
        scenes = apply_review(detections, events)
        self.assertEqual(len(scenes), 1)
        scene = scenes[0]
        self.assertEqual(scene.review_state, REVIEW_WITHDRAWN)
        self.assertFalse(scene.effective)
        self.assertTrue(scene.review_mature)  # 撤回是复核终态
        self.assertFalse(scene.valid_avoidance_result)

    def test_merge_unifies_repeated_detections_and_frame_span(self):
        detections = [
            detection("O-1", "T-1", 30, 34, outcome=OUTCOME_AVOIDED),
            detection("O-2", "T-1", 33, 40, outcome=OUTCOME_AVOIDED),
        ]
        events = [
            event("RV-1", "O-1", "confirm", new_type="water",
                  new_outcome=OUTCOME_AVOIDED),
            event("RV-2", "O-2", "merge", merge_into="O-1"),
        ]
        scenes = apply_review(detections, events)
        self.assertEqual(len(scenes), 1)
        scene = scenes[0]
        self.assertEqual(scene.detection_ids, ["O-1"])
        self.assertEqual(scene.frame_start, 30)
        self.assertEqual(scene.frame_end, 40)
        self.assertIn("RV-1", scene.corrections)
        self.assertIn("RV-2", scene.corrections)
        self.assertTrue(scene.valid_avoidance_result)

    def test_split_narrows_scene_boundary(self):
        detections = [detection("O-1", "T-1", 120, 124)]
        events = [
            event("RV-1", "O-1", "split", frame_start=122, frame_end=124),
        ]
        scenes = apply_review(detections, events)
        self.assertEqual(scenes[0].frame_start, 122)
        self.assertEqual(scenes[0].frame_end, 124)
        # 拆分本身不给出终态，场景仍未成熟
        self.assertEqual(scenes[0].review_state, REVIEW_REVISED)

    def test_split_outside_original_boundary_raises(self):
        detections = [detection("O-1", "T-1", 120, 124)]
        events = [
            event("RV-1", "O-1", "split", frame_start=110, frame_end=124),
        ]
        with self.assertRaises(ValueError):
            apply_review(detections, events)

    def test_reclassify_changes_type_and_matures_scene(self):
        # 原始为 animal，复核更正为 water —— 积水口径必须按更正后计算
        detections = [
            detection("O-1", "T-1", 10, 14, type="animal",
                      outcome=OUTCOME_INTERVENTION),
        ]
        events = [
            event("RV-1", "O-1", "reclassify", new_type="water",
                  new_outcome=OUTCOME_AVOIDED),
        ]
        scenes = apply_review(detections, events)
        scene = scenes[0]
        self.assertEqual(scene.type, "water")
        self.assertEqual(scene.outcome, OUTCOME_AVOIDED)
        self.assertEqual(scene.review_state, REVIEW_CONFIRMED)
        self.assertTrue(scene.valid_avoidance_result)

    def test_outcome_correction_overwrites_earlier_conclusion(self):
        # 原始 avoided，复核改判 intervention：报告必须反映更正后的结论
        detections = [
            detection("O-1", "T-1", 10, 14, outcome=OUTCOME_AVOIDED),
        ]
        events = [
            event("RV-1", "O-1", "reclassify", new_type="water",
                  new_outcome=OUTCOME_INTERVENTION),
        ]
        scenes = apply_review(detections, events)
        self.assertEqual(scenes[0].outcome, OUTCOME_INTERVENTION)
        self.assertTrue(scenes[0].valid_avoidance_result)

    def test_unclear_confirmed_scene_is_not_valid_avoidance_result(self):
        detections = [
            detection("O-1", "T-1", 10, 14, outcome=OUTCOME_UNCLEAR),
        ]
        events = [
            event("RV-1", "O-1", "confirm", new_type="water",
                  new_outcome=OUTCOME_UNCLEAR),
        ]
        scenes = apply_review(detections, events)
        scene = scenes[0]
        self.assertEqual(scene.review_state, REVIEW_CONFIRMED)
        self.assertTrue(scene.review_mature)
        self.assertFalse(scene.definitive_outcome)
        self.assertFalse(scene.valid_avoidance_result)

    def test_pending_scene_never_counts_as_valid_result(self):
        detections = [
            detection("O-1", "T-1", 10, 14, review_state=REVIEW_PENDING,
                      outcome=OUTCOME_AVOIDED),
        ]
        scenes = apply_review(detections, [])
        self.assertEqual(scenes[0].review_state, REVIEW_PENDING)
        self.assertFalse(scenes[0].review_mature)
        self.assertFalse(scenes[0].valid_avoidance_result)

    def test_revised_scene_without_confirmation_stays_immature(self):
        detections = [
            detection("O-1", "T-1", 10, 14, review_state=REVIEW_REVISED,
                      outcome=OUTCOME_AVOIDED),
        ]
        scenes = apply_review(detections, [])
        self.assertFalse(scenes[0].review_mature)
        self.assertFalse(scenes[0].valid_avoidance_result)

    def test_events_replayed_in_reviewed_at_order(self):
        # 后到的 reject 必须能撤回此前 confirm 的检测
        detections = [detection("O-1", "T-1", 10, 14)]
        events = [
            event("RV-2", "O-1", "reject", reviewed_at=dt(14)),
            event("RV-1", "O-1", "confirm", new_type="water",
                  new_outcome=OUTCOME_AVOIDED, reviewed_at=dt(12)),
        ]
        scenes = apply_review(detections, events)
        self.assertEqual(scenes[0].review_state, REVIEW_WITHDRAWN)

    def test_event_on_terminated_detection_raises(self):
        detections = [detection("O-1", "T-1", 10, 14)]
        events = [
            event("RV-1", "O-1", "reject", reviewed_at=dt(12)),
            event("RV-2", "O-1", "confirm", reviewed_at=dt(13),
                  new_type="water", new_outcome=OUTCOME_AVOIDED),
        ]
        with self.assertRaises(ValueError):
            apply_review(detections, events)

    def test_merge_into_unknown_detection_raises(self):
        detections = [detection("O-1", "T-1", 10, 14)]
        events = [
            event("RV-1", "O-1", "merge", merge_into="O-999"),
        ]
        with self.assertRaises(ValueError):
            apply_review(detections, events)

    def test_repeated_detection_counts_once_in_water_caliber(self):
        # 同一积水障碍出现在 3 个重叠帧检测中，合并后只有 1 个积水场景
        detections = [
            detection("O-1", "T-1", 10, 12),
            detection("O-2", "T-1", 11, 13),
            detection("O-3", "T-1", 12, 15),
        ]
        events = [
            event("RV-1", "O-1", "confirm", new_type="water",
                  new_outcome=OUTCOME_AVOIDED),
            event("RV-2", "O-2", "merge", merge_into="O-1"),
            event("RV-3", "O-3", "merge", merge_into="O-1"),
        ]
        scenes = apply_review(detections, events)
        self.assertEqual(len(scenes), 1)
        self.assertEqual(scenes[0].type, "water")
        self.assertEqual(scenes[0].detection_ids, ["O-1"])
        self.assertTrue(scenes[0].valid_avoidance_result)


if __name__ == "__main__":
    unittest.main()
