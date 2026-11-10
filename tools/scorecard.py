"""Command-line entry point for the run scorecard."""

import sys
from engine.scorecard import main


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
