#!/usr/bin/env python3
"""Self-verifying board append for dogfood run 2026-10-23 (gitreins).

Asserts before writing: board parses, trailing newline present, each target id
ABSENT. Appends rows. Re-verifies: line delta == N rows, ids each appear once,
prefix preserved. Prints DONE <n> or FAILS without writing.
"""

import json
import pathlib
import sys

BOARD = pathlib.Path("/home/kara/gitreins/.coding-hermes/board/tasks.jsonl")
ROWS = pathlib.Path("/tmp/df-rows-canon-20261023.jsonl")

board_bytes = BOARD.read_bytes()
snapshot = board_bytes

# invariant: trailing newline
if not board_bytes.endswith(b"\n"):
    print("FAIL: board lacks trailing newline; aborting")
    sys.exit(1)

# snapshot id set
snap_ids = []
for line in board_bytes.decode().splitlines():
    if line.strip():
        r = json.loads(line)
        snap_ids.append(r["id"])

new_rows = [json.loads(line) for line in ROWS.read_text().splitlines() if line.strip()]
new_ids = [r["id"] for r in new_rows]

# assert absence
for i in new_ids:
    if i in snap_ids:
        print(f"FAIL: id {i} already on board; aborting")
        sys.exit(1)

# assert duplicate-free within batch
if len(new_ids) != len(set(new_ids)):
    print("FAIL: duplicate ids within batch")
    sys.exit(1)

out = board_bytes if board_bytes.endswith(b"\n") else board_bytes + b"\n"
for r in new_rows:
    out += (json.dumps(r, ensure_ascii=False) + "\n").encode()

BOARD.write_bytes(out)

# re-verify
after = BOARD.read_bytes()
assert after[: len(snapshot)] == snapshot, "prefix violated"
tail_lines = [
    line for line in after.decode()[len(snapshot.decode()) :].splitlines() if line.strip()
]
tail_ids = [json.loads(line)["id"] for line in tail_lines]
assert tail_ids == new_ids, f"tail mismatch: {tail_ids}"
all_ids = snap_ids + new_ids
assert len(all_ids) == len(set(all_ids)), "duplicate after append"
print(f"DONE {len(new_ids)} rows appended; ids: {new_ids}")
print(f"board rows: {len(snap_ids)} -> {len(all_ids)}")
