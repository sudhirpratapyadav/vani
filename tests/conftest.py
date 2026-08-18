"""Make the tests exercise the repo source, not an installed copy.

Without this, `import vani` resolves to whatever `pip install` last put in
site-packages, and the suite silently tests stale code — which has happened.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
