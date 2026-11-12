#!/bin/bash
# foamy-github's own tests; tests/validate.sh runs this after the linters.
#   --ci    no Quickshell, no Omarchy host: the node tests and the Python unit
#           suite, which runs the helper over temporary Git repositories and
#           kit/clipboard.sh's shell cases (test_clipboard's QML case skips
#           itself; the whole test skips without the kit's hostenv)
#   --fast  skip the offscreen Quickshell runtime tests
# The other architecture's helper runs the unit suite too when this machine
# can execute it (qemu-user binfmt on x86_64), with core dumps off.
set -uo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1
ci=0 fast=0
for arg in "$@"; do
  case $arg in
    --ci) ci=1 ;;
    --fast) fast=1 ;;
    *)
      echo "suite.sh: unknown argument $arg" >&2
      exit 2
      ;;
  esac
done
status=0
run() {
  echo "== $*"
  "$@" || status=1
}
skip() { echo "SKIP $*"; }
# test_clipboard.py's QML case skips itself when this is set.
if ((ci)); then export FOAMY_SUITE_CI=1; fi

run node --test tests/model.test.js tests/preferences.test.js
run node tests/i18n.test.cjs
run python3 -I -m unittest discover -s tests -p 'test_*.py'

other=aarch64
[[ $(uname -m) == aarch64 ]] && other=x86_64
helper="$PWD/bin/$other/foamy-github-helper"
# -h prints the usage (and touches nothing), when this machine can run it at all.
if "$helper" -h 2>/dev/null | grep -q '^usage: foamy-github-helper'; then
  (
    ulimit -c 0
    FOAMY_HELPER="[\"$helper\"]" run python3 -I -m unittest discover -s tests -p 'test_*.py'
    exit $status
  ) || status=1
else
  skip "the $other helper: this machine can't run it (no qemu-user binfmt)"
fi

if ((ci || fast)); then
  skip "tests/runtime-*.py and the kit's i18n_runtime.py: offscreen Quickshell"
else
  for test in panel pullall watcher stale-status lifecycle stored; do
    run python3 "tests/runtime-$test.py"
  done
  if [[ -n ${FOAMY_KIT:-} ]]; then
    run python3 "$FOAMY_KIT/preview/i18n_runtime.py" .
  else
    skip "the kit's i18n_runtime.py: FOAMY_KIT is not set"
  fi
fi
exit $status
