"""Launcher used by the plugin manifest and by hand: `python run.py [serve|status|bench|init]`."""

import os
import sys

if sys.version_info < (3, 11):
    sys.stderr.write(
        f"routeai needs Python 3.11 or newer, but {sys.executable} is {sys.version.split()[0]}.\n"
        "Install a newer Python (python.org, Homebrew, your package manager) and make sure the plugin's "
        "python command points to it.\n"
    )
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from routeai.__main__ import main  # noqa: E402

sys.exit(main())
