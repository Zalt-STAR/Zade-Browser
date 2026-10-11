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
#   SHARD_INDEX / SHARD_TOTAL
#               when set, compile only this machine's share of the object files
#               and leave the objects it added to the compiler cache in
#               $CCACHE_DELTA_DIR, so parallel machines can pool their work
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

# In shard mode, pick this machine's share of the object files the APK needs.
SHARD_INDEX="${SHARD_INDEX:--1}"
SHARD_TOTAL="${SHARD_TOTAL:-1}"
targets_file=""
shard_targets() {
    local ninja="$src/third_party/ninja/ninja"
    [ -x "$ninja" ] || ninja="$(command -v ninja)"
    (cd "$src" && "$ninja" -C out/Default -t inputs chrome_public_apk) > "$ROOT/inputs.txt" || return 1
    grep -E '(^|/)obj/.*\.o$' "$ROOT/inputs.txt" | sort -u > "$ROOT/objects.txt"
    local total
    total="$(wc -l < "$ROOT/objects.txt")"
    echo "object files needed by the APK: $total"
    [ "$total" -ge 10000 ] || return 1
    awk -v n="$SHARD_TOTAL" -v i="$SHARD_INDEX" 'NR % n == i' "$ROOT/objects.txt" > "$ROOT/targets.txt"
    echo "this shard ($SHARD_INDEX of $SHARD_TOTAL): $(wc -l < "$ROOT/targets.txt")"
}
# Used only if the object list cannot be produced: big targets, verified to exist.
fallback_targets() {
    local -a groups=(
        "third_party/blink/renderer/core:core"
        "third_party/blink/renderer/modules:modules"
        "third_party/blink/renderer/platform:platform"
        "chrome/browser:browser"
        "chrome/browser/ui:ui chrome/renderer:renderer"
        "content/browser:browser storage/browser:browser"
        "skia:skia net:net media:media services/network:network_service"
        "cc:cc gpu:gpu ui/gfx:gfx components/viz/service:service"
    )
    echo "${groups[$(( SHARD_INDEX % ${#groups[@]} ))]}" | tr ' ' '\n' > "$ROOT/targets.txt"
    echo "fallback targets: $(tr '\n' ' ' < "$ROOT/targets.txt")"
}
cache_files() { (cd "$CCACHE_DIR" 2>/dev/null && find . -type f ! -name stats ! -name '*.lock' ! -path './tmp/*' | sort) || true; }
if [ "$SHARD_INDEX" -ge 0 ]; then
    step shardlist shard_targets || step shardlist-fallback fallback_targets || fail shardlist
    targets_file="$ROOT/targets.txt"
    cache_files > "$ROOT/cache_before.txt"
fi

elapsed=$(( $(date +%s) - JOB_START ))
budget=$(( JOB_MINUTES * 60 - RESERVE_MINUTES * 60 - elapsed ))
echo "compile budget: $(( budget / 60 )) minutes (clang $clang_rev)" | tee -a "$LOGS/timeline.txt"

if [ "$budget" -lt 600 ]; then
    echo timeout > "$LOGS/status.txt"
else
    BUILD_TARGETS_FILE="$targets_file" BUILD_TIMEOUT="$budget" NINJA_ARGS="-k 0" step build build build
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

# Shard mode: hand over only the cache objects this machine added.
if [ "$SHARD_INDEX" -ge 0 ]; then
    delta="${CCACHE_DELTA_DIR:-$HOME/ccdelta}"
    mkdir -p "$delta"
    cache_files > "$ROOT/cache_after.txt"
    comm -13 "$ROOT/cache_before.txt" "$ROOT/cache_after.txt" | grep -v '^./ccache.conf$' > "$ROOT/cache_new.txt"
    tar -C "$CCACHE_DIR" -cf - -T "$ROOT/cache_new.txt" | tar -C "$delta" -xf -
    echo "cache delta: $(wc -l < "$ROOT/cache_new.txt") files, $(du -sh "$delta" | cut -f1)" | tee -a "$LOGS/timeline.txt"
fi

# Build statistics, for working out where the time goes.
for f in siso_metrics.json .siso_metrics.json; do
    if [ -f "$src/out/Default/$f" ]; then
        zstd -q -19 -T0 -o "$LOGS/siso_metrics.json.zst" "$src/out/Default/$f" || true
    fi
done

# -vv lists why calls were uncacheable, which is what matters when hits are low.
ccache -s -vv > "$LOGS/ccache.txt" 2>&1 || true
cp "$src/out/Default/args.gn" "$LOGS/args.gn" 2>/dev/null || true
exit 0
