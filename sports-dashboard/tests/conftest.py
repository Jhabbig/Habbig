"""Shared test fixtures.

We force DEV_MODE=1 before importing sports_dashboard so the security
middleware doesn't fail-closed for tests that hit the FastAPI app.
"""
import os
import sys
from pathlib import Path

os.environ["DEV_MODE"] = "1"

# Make the parent directory importable so `import sports_dashboard` works
# regardless of where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
