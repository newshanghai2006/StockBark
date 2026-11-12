"""Optional local workaround for Python 3.14 hosts whose Windows WMI hangs.

Usage: python tools/windows_python.py unittest discover -s tests -v
Not used by the application or Docker image.
"""

import platform
import runpy
import sys
from pathlib import Path


if sys.platform == "win32" and hasattr(platform, "_wmi"):
    platform._wmi = None
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if len(sys.argv) < 2:
    sys.exit("Specify a Python module to run")
sys.argv = sys.argv[1:]
runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
