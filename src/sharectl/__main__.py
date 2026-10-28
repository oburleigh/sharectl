"""Run the command line as `python -m sharectl`, the way `play` starts a controller."""

import sys

from sharectl.cli import main

sys.exit(main())
