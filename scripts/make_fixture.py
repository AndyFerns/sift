"""Create the shared fixture workspace somewhere you can poke at it by hand.

    python scripts/make_fixture.py /tmp/sift-fixture

The directory must not exist yet (or must be empty). The fixture is the same tree the
test suite uses: a handful of small projects with artifacts of known type and size. Pass
--expected to print what a correct scan of it must report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from fixture_workspace import build_workspace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--expected", action="store_true", help="print the expected artifacts")
    args = parser.parse_args()

    target: Path = args.path
    if target.exists() and any(target.iterdir()):
        print(f"error: {target} already exists and is not empty", file=sys.stderr)
        return 1
    workspace = build_workspace(target)
    if args.expected:
        expected = [e.as_dict() for e in workspace.expect(recursive=True).values()]
        print(json.dumps(expected, indent=2))
    else:
        print(f"Fixture created at {target.resolve()}")
        print(f"  {len(workspace.expect(recursive=False))} artifacts in a normal scan")
        print(f"  {len(workspace.expect(recursive=True))} artifacts in a recursive scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
