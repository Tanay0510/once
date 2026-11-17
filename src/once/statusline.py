"""Entry point for Claude Code's status line: `python -m once.statusline`."""
import sys

from .ambient import statusline_main

if __name__ == "__main__":
    sys.exit(statusline_main())
