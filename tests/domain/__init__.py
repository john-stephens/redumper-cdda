"""Domain-model tests."""

import sys
from pathlib import Path

SRC_PATH = Path(__file__).parents[2] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))
