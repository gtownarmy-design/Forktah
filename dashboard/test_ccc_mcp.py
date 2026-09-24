"""Gate entry for the ccc-board plugin's suite, which lives with the plugin.

Every suite run_tests.sh registers is a dashboard/test_* file: test_residue.sh swaps
each one for a stub when it proves the runner, and a suite registered by a path
outside dashboard/ would run for real inside that fixture and fail it.
"""
import runpy
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "plugins" / "ccc-board" / "tests" / "test_ccc_mcp.py"
sys.argv[0] = str(TARGET)
runpy.run_path(str(TARGET), run_name="__main__")
