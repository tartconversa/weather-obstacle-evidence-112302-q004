from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests" / "test_materials.py"), run_name="__main__")
