"""Run the same suite with reported host free space below any production reserve."""
from types import SimpleNamespace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
suite = unittest.defaultTestLoader.discover(str(root / "tests"))
with patch("shutil.disk_usage", return_value=SimpleNamespace(free=512_000)):
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
