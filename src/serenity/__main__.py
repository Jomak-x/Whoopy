"""Allow `python -m serenity` to behave like the installed CLI."""

from serenity.cli import main

raise SystemExit(main())
