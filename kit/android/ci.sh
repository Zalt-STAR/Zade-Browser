#!/usr/bin/env bash
# One unattended build pass on a small CI machine (GitHub-hosted runner).
#
# A full Chromium compile does not fit in one 6-hour job, so each pass redoes
# the cheap setup, compiles until a deadline, and saves the compiler cache.
# The next pass picks the cache up and gets further.
#
# Writes $LOGS/status.txt: done | timeout | failed, plus per-stage logs.
# Always exits 0; the caller decides what to do with the status.
#
# Environment:
#   ROOT        work directory (default: $HOME/build)
#   JOB_START   epoch seconds when the job began (default: now)
#   JOB_MINUTES total minutes the job may run (default: 355)
set -uo pipefail

kit="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"
ROOT="${ROOT:-$HOME/build}"
LOGS="$ROOT/logs"
JOB_START="${JOB_START:-$(date +%s)}"
JOB_MINUTES="${JOB_MINUTES:-355}"
# Stop compiling this long before the job limit, to save the cache and publish logs.
RESERVE_MINUTES=35

mkdir -p "$LOGS"
: > "$LOGS/timeline.txt"
echo failed > "$LOGS/status.txt"

slug="$(python3 -c "import sys, tomllib; print(tomllib.load(open(sys.argv[1], 'rb'))['fork']['slug'])" "$kit/fork.toml")"
fork="$ROOT/$slug-linux"
work="$ROOT/$slug-android"
src="$work/src"

step() {
    local name="$1"; shift
    local start rc
    start="$(date +%s)"
    echo "::group::$name"
    "$@" > >(tee "$LOGS/$name.log") 2>&1
    rc=$?
    sleep 1  # let tee flush
    echo "::endgroup::"
    echo "$name rc=$rc seconds=$(( $(date +%s) - start )) disk_free=$(df -h --output=avail / | tail -1 | tr -d ' ')" \
        | tee -a "$LOGS/timeline.txt"
    return "$rc"
}

fail() {
    echo "$1" > "$LOGS/failed_stage.txt"
    echo failed > "$LOGS/status.txt"
    exit 0
}

build() { "$kit/android/build.sh" "$fork" "$1"; }

do_fork() { mkdir -p "$ROOT" && cd "$ROOT" && rm -rf "$fork" && "$kit/fork.sh" "$kit/fork.toml"; }

step fork do_fork || fail fork
step fetch build fetch || fail fetch
# Missing system packages show up as clearer errors later, so do not stop here.
DEPS_ARGS="--android --no-prompt --no-chromeos-fonts --no-syms --no-nacl" \
    step deps build deps || echo "deps reported a problem; continuing" | tee -a "$LOGS/timeline.txt"
step hooks build hooks || fail hooks
step helium build helium || fail helium
step brand build brand || fail brand
GN_PROFILE=ci step gn build gn || fail gn

# Compiler cache: key on the compiler's revision rather than its timestamp,
# because every pass downloads a fresh copy of the same compiler.
clang_rev="$(cat "$src/third_party/llvm-build/Release+Asserts/cr_build_revision" 2>/dev/null || echo unknown)"
export CCACHE_DIR="${CCACHE_DIR:-$HOME/ccache}"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-9G}"
export CCACHE_COMPILERCHECK="string:$clang_rev"
export CCACHE_SLOPPINESS="time_macros,include_file_mtime,include_file_ctime"
export CCACHE_NOHASHDIR=1
ccache -z > /dev/null 2>&1 || true

elapsed=$(( $(date +%s) - JOB_START ))
budget=$(( JOB_MINUTES * 60 - RESERVE_MINUTES * 60 - elapsed ))
echo "compile budget: $(( budget / 60 )) minutes (clang $clang_rev)" | tee -a "$LOGS/timeline.txt"

if [ "$budget" -lt 600 ]; then
    echo timeout > "$LOGS/status.txt"
else
    BUILD_TIMEOUT="$budget" NINJA_ARGS="-k 0" step build build build
    rc=$?
    if [ "$rc" -eq 0 ]; then
        echo done > "$LOGS/status.txt"
    elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        echo timeout > "$LOGS/status.txt"
    else
        echo build > "$LOGS/failed_stage.txt"
        echo failed > "$LOGS/status.txt"
    fi
fi

ccache -s > "$LOGS/ccache.txt" 2>&1 || true
cp "$src/out/Default/args.gn" "$LOGS/args.gn" 2>/dev/null || true
exit 0
