"""Module entry point for ``python -m gitreins``.

`gitreins install` / `gitreins init` pin the *installing* interpreter into the
generated pre-commit hook — as an absolute console-script path when one
launched the command, otherwise as ``<sys.executable> -m gitreins`` (DF-011).
That second form needs an executable module here: without it every hook
written that way dies with "No module named gitreins.__main__; 'gitreins' is a
package and cannot be directly executed", which turns the pinned guard into a
gate that blocks every commit instead of running the checks (DF-024).

Keep this module free of side effects beyond delegating to ``cli.main``.
"""

import sys

from gitreins.cli import main

if __name__ == "__main__":
    sys.exit(main())
