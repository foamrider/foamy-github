#!/usr/bin/env bash
# Run the Python suite repeatedly to catch timing-dependent failures.
#   bash tests/stress-python.sh [runs=40] [parallel=4] [logdir=/tmp/foamy-github-stress]
# Each run's output is kept as <logdir>/run-N.log; failures are listed at the end.
set -u
runs=${1:-40}
parallel=${2:-4}
logs=${3:-/tmp/foamy-github-stress}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$logs"
rm -f "$logs"/run-*.log "$logs"/status.txt
# shellcheck disable=SC2016  # $1..$3 expand in the inner sh
seq 1 "$runs" | xargs -P "$parallel" -I{} sh -c \
  'cd "$1" && python3 -m unittest discover -s tests -p "test_*.py" > "$2/run-$3.log" 2>&1; echo "$3 $?" >> "$2/status.txt"' _ "$root" "$logs" {}
failed=$(awk '$2 != 0 {print $1}' "$logs/status.txt" | sort -n)
echo "runs: $(wc -l < "$logs/status.txt"), failed: $(echo "$failed" | grep -c . || true)"
for n in $failed; do
  echo "== run $n"
  grep -E -A20 '^(ERROR|FAIL):' "$logs/run-$n.log" | head -40
done
[ -z "$failed" ]
