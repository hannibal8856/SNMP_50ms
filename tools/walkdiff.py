#!/usr/bin/env python3
"""Compare two snmpwalk captures and classify the differences.

Used as the verification loop for Plan E: every phase must show
`mainline - planE == empty` and no value drift on shared OIDs.
"""
import re
import sys

_LINE = re.compile(r"^\s*(?:iso([0-9.]+)|\.?(1(?:\.[0-9]+)+))\s*=\s*(.*?)\s*$")
_TERMINATORS = ("End of MIB", "No more variables left in this MIB View")


def parse_walk(path):
    """Return ({oid: value_text}, completed_normally)."""
    oids = {}
    last = ""
    last_oid = None
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.strip():
                last = line.strip()
            m = _LINE.match(line)
            if m:
                oid = ("1" + m.group(1)) if m.group(1) else m.group(2)
                oids[oid] = m.group(3)
                last_oid = oid
            elif line.strip() and not any(line.startswith(t) for t in _TERMINATORS):
                # Continuation line: append to the last OID's value
                if last_oid is not None:
                    oids[last_oid] += " " + line.strip()
    return oids, any(last.startswith(t) for t in _TERMINATORS)


def diff(a, b):
    """Return (only_in_a, only_in_b, [(oid, a_value, b_value), ...])."""
    ka, kb = set(a), set(b)
    differs = [(o, a[o], b[o]) for o in sorted(ka & kb) if a[o] != b[o]]
    return ka - kb, kb - ka, differs


def load_profile(path):
    """Return (private_mib_names, standard_mib_names) from a product .profile."""
    private, standard, bucket = set(), set(), None
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("###"):
                bucket = private if "Private" in line else standard
                continue
            if "=" in line and bucket is not None:
                name, _, value = line.partition("=")
                if value.strip().upper() == "YES":
                    bucket.add(name.strip())
    return private, standard


def _sort_key(oid):
    return [int(x) for x in oid.split(".")]


def main(argv):
    if len(argv) != 3:
        print("usage: walkdiff.py <mainline.txt> <plane.txt>", file=sys.stderr)
        return 2
    a, a_ok = parse_walk(argv[1])
    b, b_ok = parse_walk(argv[2])
    for path, ok in ((argv[1], a_ok), (argv[2], b_ok)):
        if not ok:
            print(f"WARNING: {path} did not terminate normally "
                  f"(no 'End of MIB') - capture may be truncated")
    only_a, only_b, differs = diff(a, b)
    print(f"mainline={len(a)} planE={len(b)} "
          f"missing={len(only_a)} surplus={len(only_b)} value_differs={len(differs)}")
    for o in sorted(only_a, key=_sort_key):
        print(f"  MISSING  {o} = {a[o]}")
    for o in sorted(only_b, key=_sort_key):
        print(f"  SURPLUS  {o} = {b[o]}")
    for o, va, vb in differs:
        print(f"  DIFFERS  {o}: {va!r} -> {vb!r}")
    return 1 if (only_a or differs) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
