"""CLI entry point for memento-teams."""

import os
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    from tui_app import MementoTeams

    MementoTeams().run()


if __name__ == "__main__":
    main()
