import sys
from pathlib import Path

# Add src to path for src/briefing.py's internal imports
sys.path.insert(0, str(Path(__file__).parent / "src"))
