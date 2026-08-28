"""Package entry point so ``python -m vacode`` works without installing."""

from .cli import main

raise SystemExit(main())
