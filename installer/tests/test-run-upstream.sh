#!/bin/bash
# Self-check for lib.sh's run_upstream. Run directly: ./installer/tests/test-run-upstream.sh
#
# Covers the two things easy to break in that function: the upstream's output
# must reach the terminal live (not just a log), and its exit code must survive
# the `| tee` pipe. A plain `$?` there yields tee's status, which is always 0 —
# i.e. every failed update would report success, the exact bug the fetch-to-file
# rewrite was meant to end.
#
# Deliberately runs with pipefail OFF. Both callers happen to set it, which
# masks a `$?` regression entirely; lib.sh is sourced, so it has to hold on its
# own. With pipefail on, this file passes even against the bug it exists to catch.
set -u
set +o pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source ./lib.sh

fails=0
check() { if [ "$2" = "$3" ]; then echo "  ok: $1"; else echo "  FAIL: $1 (want '$3', got '$2')"; fails=$((fails+1)); fi; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
# mingw curl (git-bash) resolves file:// against Windows paths, not the msys
# ones mktemp hands out. Only affects running this check on a dev box.
url_for() { if command -v cygpath >/dev/null 2>&1; then echo "file:///$(cygpath -m "$1")"; else echo "file://$1"; fi; }
# Pinned, not left to lazy creation: the calls below run in $( ), so a log path
# the function assigned itself would die with the subshell.
UPSTREAM_LOG="$work/upstream.log"
printf '#!/bin/bash\necho MARKER_STDOUT\necho MARKER_STDERR >&2\nexit 0\n' > "$work/good.sh"
printf '#!/bin/bash\necho MARKER_STDOUT\nexit 7\n'                          > "$work/bad.sh"

# success: rc 0, and the upstream's stdout+stderr both reached our stdout
out=$(run_upstream "good" "$(url_for "$work/good.sh")" 2>/dev/null); rc=$?
check "successful upstream returns 0"        "$rc" "0"
check "upstream stdout reaches the terminal" "$(grep -c MARKER_STDOUT <<<"$out")" "1"
check "upstream stderr reaches the terminal" "$(grep -c MARKER_STDERR <<<"$out")" "1"

# failure: the exit code must survive the pipe, not be masked by tee
out=$(run_upstream "bad" "$(url_for "$work/bad.sh")" 2>/dev/null); rc=$?
check "failing upstream returns non-zero (no pipefail)" "$rc" "1"
check "output still shown on failure"        "$(grep -c MARKER_STDOUT <<<"$out")" "1"

# unfetchable URL must fail, not silently succeed
run_upstream "missing" "$(url_for "$work/nope.sh")" >/dev/null 2>&1; rc=$?
check "unfetchable url returns non-zero"     "$rc" "1"

# the log is still written for after-the-fact diagnosis
check "log captured both runs" "$(grep -c MARKER_STDOUT "$UPSTREAM_LOG")" "2"

[ "$fails" -eq 0 ] && { echo "all checks passed"; exit 0; }
echo "$fails check(s) failed"; exit 1
