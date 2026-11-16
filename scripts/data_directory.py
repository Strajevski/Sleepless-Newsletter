"""Print the configured runtime directory without creating or moving files."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from circus_newsletter.settings import data_directory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="Installation folder containing config/settings.json")
    print(data_directory(parser.parse_args().root.resolve()))


if __name__ == "__main__":
    main()
