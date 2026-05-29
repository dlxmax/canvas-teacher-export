"""Make the top-level canvas_archive module importable from the test suite."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
