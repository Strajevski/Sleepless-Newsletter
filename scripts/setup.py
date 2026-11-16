"""Create a local environment and guide configuration; automation is enabled separately."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.bootstrap import main


if __name__ == "__main__":
    raise SystemExit(main())
