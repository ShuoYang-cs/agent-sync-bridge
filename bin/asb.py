"""Agent Sync Bridge entry point for every platform: python -X utf8 -I bin/asb.py <args>."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from asb.cli import main  # noqa: E402

sys.exit(main())
