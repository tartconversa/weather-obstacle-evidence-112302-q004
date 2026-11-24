"""CSV 导入校验与 CLI 端到端测试（使用仓库内合成 data/ 目录）。"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import SRC

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from low_visibility_obstacle_evidence.cli import main  # noqa: E402
from low_visibility_obstacle_evidence.io_csv import (  # noqa: E402
    load_dataset,
    parse_utc,
)


class ImportTests(unittest.TestCase):
    def test_load_full_dataset(self):
        dataset = load_dataset(DATA)
        self.assertEqual(len(dataset.visibility), 47)
        self.assertEqual(len(dataset.drives), 50)
        self.assertEqual(len(dataset.detections), 30)
        self.assertEqual(len(dataset.windows), 24)
        # 可选资料已由生成脚本写入
        self.assertGreater(len(dataset.review_events), 0)
        self.assertGreater(len(dataset.closures), 0)
        self.assertGreater(len(dataset.route_coverage), 0)
        for filename, digest in dataset.source_files.items():
            self.assertEqual(len(digest), 64)

    def test_missing_required_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(DATA / "drives.csv", tmp)
            with self.assertRaises(FileNotFoundError):
                load_dataset(tmp)

    def test_bad_enum_value_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("visibility.csv", "drives.csv",
                         "obstacles.csv", "test_windows.csv"):
                shutil.copy(DATA / name, tmp)
            path = Path(tmp) / "visibility.csv"
            with path.open(encoding="utf-8-sig") as source:
                rows = list(csv.DictReader(source))
            rows[0]["observation_quality"] = "bogus"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaises(ValueError) as ctx:
                load_dataset(tmp)
            self.assertIn("observation_quality", str(ctx.exception))

    def test_foreign_key_violation_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("visibility.csv", "drives.csv",
                         "obstacles.csv", "test_windows.csv"):
                shutil.copy(DATA / name, tmp)
            path = Path(tmp) / "obstacles.csv"
            with path.open(encoding="utf-8-sig") as source:
                rows = list(csv.DictReader(source))
            rows[0]["trip_id"] = "T-999"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaises(ValueError) as ctx:
                load_dataset(tmp)
            self.assertIn("T-999", str(ctx.exception))

    def test_parse_utc_handles_z_and_offset(self):
        from datetime import timezone
        first = parse_utc("2026-11-02T01:00Z", field="x", row_no=2,
                          filename="f.csv")
        second = parse_utc("2026-11-02T09:00:00+08:00", field="x",
                           row_no=2, filename="f.csv")
        self.assertEqual(first.utcoffset(), timezone.utc.utcoffset(first))
        self.assertEqual(first, second)


class CliEndToEndTests(unittest.TestCase):
    def test_analyze_and_plan_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = main([
                "analyze", "--data-dir", str(DATA),
                "--out-dir", tmp,
            ])
            self.assertEqual(code, 0)
            payload = json.loads(
                (Path(tmp) / "analysis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["versions"], ["V14", "V15"])
            # 三口径键必须存在且语义独立
            for version in payload["versions"]:
                caliber = payload["calibers"][version]
                self.assertIn("low_visibility_trips", caliber)
                self.assertIn("water_scenes", caliber)
                self.assertIn("valid_avoidance_results", caliber)
            report = (Path(tmp) / "analysis_report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("无证据", report)
            self.assertIn("不可外推", report)

            code = main([
                "plan", "--data-dir", str(DATA),
                "--out-dir", tmp,
                "--as-of", "2026-11-22T00:00Z",
            ])
            self.assertEqual(code, 0)
            plan = json.loads(
                (Path(tmp) / "retest_plan.json").read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(
                plan["summary"]["total_targets"],
                plan["summary"]["scheduled"] + plan["summary"]["unmet"],
            )
            self.assertTrue((Path(tmp) / "retest_plan.csv").exists())

    def test_reevaluate_blocks_after_closure(self):
        with tempfile.TemporaryDirectory() as tmp:
            main(["plan", "--data-dir", str(DATA), "--out-dir", tmp,
                  "--as-of", "2026-11-22T00:00Z"])
            # RT-3 自 2026-11-25 封闭；26 日重评估应阻断其上未执行条目
            code = main([
                "reevaluate", "--data-dir", str(DATA),
                "--plan-json", str(Path(tmp) / "retest_plan.json"),
                "--out-dir", tmp,
                "--as-of", "2026-11-26T00:00Z",
            ])
            self.assertEqual(code, 0)
            plan = json.loads(
                (Path(tmp) / "retest_plan.json").read_text(encoding="utf-8")
            )
            rt3_entries = [
                e for e in plan["entries"] if e["route_id"] == "RT-3"
            ]
            if rt3_entries:
                self.assertTrue(
                    all(e["status"] == "blocked" for e in rt3_entries)
                )

    def test_reevaluate_rejects_changed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as mutated:
            main(["plan", "--data-dir", str(DATA), "--out-dir", tmp])
            for name in ("visibility.csv", "drives.csv",
                         "obstacles.csv", "test_windows.csv",
                         "weather.csv", "review_events.csv",
                         "closures.csv", "route_coverage.csv"):
                shutil.copy(DATA / name, mutated)
            # 篡改一条能见度记录，校验和随之变化
            path = Path(mutated) / "visibility.csv"
            with path.open(encoding="utf-8-sig") as source:
                rows = list(csv.DictReader(source))
            rows[0]["visibility_km"] = "0.1"
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            with self.assertRaises(SystemExit):
                main([
                    "reevaluate", "--data-dir", mutated,
                    "--plan-json", str(Path(tmp) / "retest_plan.json"),
                    "--out-dir", tmp,
                    "--as-of", "2026-11-26T00:00Z",
                ])

    def test_module_invocation_runs(self):
        # 在空目录下调用模块入口，验证不依赖当前工作目录；
        # 不使用 -I，因为隔离模式会忽略 PYTHONPATH，而包位于 src 布局下。
        with tempfile.TemporaryDirectory() as empty_cwd:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(SRC)
            result = subprocess.run(
                [sys.executable, "-m",
                 "low_visibility_obstacle_evidence", "--help"],
                env=env, capture_output=True, text=True, check=False,
                cwd=empty_cwd,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("analyze", result.stdout)


if __name__ == "__main__":
    unittest.main()
