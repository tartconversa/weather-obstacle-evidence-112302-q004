# 低能见度道路障碍的覆盖解释与补测安排

用于本地分析开发的合成车辆运营资料与评估应用。所有车辆、地区、道路、
调查参与者及事件标识均为虚构值；评估需求来自公开功能说明，不代表新闻
报道过相关事故或故障。

应用回答三个问题：

1. 不同道路能见度与路况下，现有资料**支持什么结论**（三口径严格分离）；
2. 哪些区域/分级组合**没有证据**，不能报告成表现正常；
3. 值得补测的路段如何匹配到**合规窗口、传感器配置与通行时段**，并在
   共享窗口争用与道路封闭时保持可解释。

## 快速开始

Python 3.11 至 3.14 可创建独立环境：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

# 分析 + 补测排程
python -m low_visibility_obstacle_evidence plan \
    --data-dir data --out-dir out --as-of 2026-11-22T00:00Z

# 道路封闭或许可变化后重评估未执行条目
python -m low_visibility_obstacle_evidence reevaluate \
    --data-dir data --plan-json out/retest_plan.json \
    --out-dir out --as-of 2026-11-26T00:00Z
```

Windows PowerShell 可使用 `.venv\Scripts\Activate.ps1` 激活环境。

## 测试

```sh
python -m unittest discover -s tests -v
```

`data/` 中的 CSV 使用 UTF-8 BOM 编码，首行是字段名。基线四个文件随仓库
固定；三个可选文件（复核事件、道路封闭、路线覆盖）由
`tools/generate_optional_samples.py` 确定性生成，重复运行结果一致。

## 文档

* [`docs/资料契约.md`](docs/资料契约.md) —— 字段含义、取值枚举与口径边界
* [`docs/应用说明.md`](docs/应用说明.md) —— 架构、业务口径、CLI 用法与测试映射

## 目录

```
src/low_visibility_obstacle_evidence/   应用包（导入/匹配/复核/分析/排程/报告/CLI）
data/                                   合成输入 CSV
tests/                                  unittest 测试（时间错位、复核更正、共享窗口争用等）
tools/                                  资料完整性检查与可选样例生成
out/                                    运行产物（报告、JSON、计划 CSV）
```
