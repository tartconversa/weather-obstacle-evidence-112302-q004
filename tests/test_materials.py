from __future__ import annotations

import csv
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MaterialTests(unittest.TestCase):
    def test_files_have_declared_headers_and_rows(self):
        expected = EXPECTED
        for filename, (columns, minimum_rows) in expected.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertGreaterEqual(len(rows), minimum_rows)
                self.assertTrue(all(None not in row for row in rows))


EXPECTED = {'visibility.csv': (['sensor_id', 'region', 'observed_at', 'visibility_km', 'observation_quality', 'coverage_fraction'], 47), 'drives.csv': (['trip_id', 'version', 'vehicle_id', 'region', 'started_at', 'ended_at', 'sensor_config', 'upload_fraction'], 50), 'obstacles.csv': (['obstacle_id', 'trip_id', 'frame_start', 'frame_end', 'type', 'review_state', 'outcome'], 30), 'test_windows.csv': (['window_id', 'region', 'route_id', 'visibility_band', 'permit', 'sensor_config', 'capacity', 'status'], 24)}


if __name__ == "__main__":
    unittest.main()
