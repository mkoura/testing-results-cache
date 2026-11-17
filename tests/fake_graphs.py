"""A stand-in for sync_static_graphs.py, for the sync analyzer tests.

Takes the arguments the analyzer passes and writes one small PNG per graph
name into the output folder. Each file holds the labels it was given, so a
test can check which runs a graph was drawn from. Every call is appended to
the file named by FAKE_GRAPHS_LOG, so a test can count renders.
"""

import argparse
import os
import sys
from pathlib import Path

# The smallest valid PNG: one grey pixel.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108000000003a7e9b55"
    "0000000a49444154789c6300010000050001ab0bb2fd0000000049454e44ae426082"
)
GRAPHS = ("nodesync_progress", "nodesync_time_per_era", "nodesync_cpu_consumption")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode")
    parser.add_argument("-i", "--json-files", nargs="+", required=True)
    parser.add_argument("--labels", nargs="+", required=True)
    parser.add_argument("-o", "--output-dir", required=True)
    args = parser.parse_args()
    if len(args.labels) != len(args.json_files):
        return 2
    for path in args.json_files:
        if not Path(path).is_file():
            return 3
    if "fail" in args.labels:
        print("asked to fail", file=sys.stderr)
        return 4
    # The real script refuses repeated labels and exits 0 with no output.
    if len(set(args.labels)) != len(args.labels) or "silent" in args.labels:
        return 0
    out = Path(args.output_dir)
    out.mkdir(parents=True)
    for name in GRAPHS:
        (out / f"{name}.png").write_bytes(PNG + " ".join(args.labels).encode())
    log = os.environ.get("FAKE_GRAPHS_LOG")
    if log:
        with Path(log).open("a", encoding="utf-8") as f:
            f.write(" ".join(args.labels) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
