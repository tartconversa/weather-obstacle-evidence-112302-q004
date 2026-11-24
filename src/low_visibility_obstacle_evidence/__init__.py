"""低能见度道路障碍规避能力评估与补测计划应用。

模块划分：

* :mod:`~low_visibility_obstacle_evidence.models` —— 领域模型与口径常量
* :mod:`~low_visibility_obstacle_evidence.io_csv` —— CSV 导入与校验
* :mod:`~low_visibility_obstacle_evidence.time_match` —— 时间错位匹配
* :mod:`~low_visibility_obstacle_evidence.review` —— 复核更正与场景重建
* :mod:`~low_visibility_obstacle_evidence.analysis` —— 覆盖矩阵与版本对比
* :mod:`~low_visibility_obstacle_evidence.planner` —— 补测排程与封闭重评估
* :mod:`~low_visibility_obstacle_evidence.reporting` —— 中文报告与导出
* :mod:`~low_visibility_obstacle_evidence.cli` —— 命令行入口
"""

from __future__ import annotations

__version__ = "0.2.0"

from .analysis import build_analysis
from .io_csv import load_dataset
from .planner import plan_retest, reevaluate_plan
from .time_match import MatchConfig, match_trip_visibility

__all__ = [
    "MatchConfig",
    "build_analysis",
    "load_dataset",
    "match_trip_visibility",
    "plan_retest",
    "reevaluate_plan",
]
