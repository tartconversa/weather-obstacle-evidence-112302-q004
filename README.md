# 低能见度道路障碍的覆盖解释与补测安排

这是用于本地分析开发的合成车辆运营资料。所有车辆、地区、道路、调查参与者及事件标识均为虚构值。

Python 3.11 至 3.14 可创建独立环境。当前基线只提供输入资料、资料契约和完整性检查，不包含分析与运营决策功能。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m unittest discover -s tests -v
```

Windows PowerShell 可使用 `.venv\Scripts\Activate.ps1` 激活环境。`data/` 中的 CSV 使用 UTF-8 BOM 编码，首行是字段名。`docs/资料契约.md` 说明记录含义、来源边界和需要保留的时间/版本信息。
