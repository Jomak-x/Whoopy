"""Allow `python -m whoopy` to behave like the installed CLI."""

from whoopy.cli import main

raise SystemExit(main())
