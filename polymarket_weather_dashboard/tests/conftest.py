"""Make the dashboard root importable so tests can `import weather_pure`
without needing the package layout. Pytest auto-discovers this file."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
