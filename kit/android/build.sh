#!/usr/bin/env bash
# Builds the fork for Android from a rebranded checkout made by ../fork.sh.
#
#   android/build.sh <slug>-linux            run every stage in order
#   android/build.sh <slug>-linux <stage>    run one stage: fetch deps hooks
#                                            helium brand gn build triage
#
# Follows Chromium's Android build guide (docs/android_build_instructions.md)
# for the checkout and compile, and Helium's own scripts/shared.sh for applying
# Helium on top. Needs an x86-64 Linux machine; Chromium does not support
# building for Android on macOS or Windows.
#
# NOT YET RUN END TO END. The checkout and compile stages need a machine this
# kit's author did not have. The Helium and branding stages were checked
# against the real Chromium 155.0.8059.39 files they touch. Expect compile
# errors on the first builds; see README.md for where and why.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
kit="$(dirname "$here")"

die() { echo "error: $*" >&2; exit 1; }
say() { echo; echo "==> $*"; }

[ $# -ge 1 ] || die "usage: $0 <slug>-linux [stage]"
fork="$(realpath "$1")"
stage="${2:-all}"
core="$fork/helium-chromium"
config="${FORK_CONFIG:-$kit/fork.toml}"

[ -f "$fork/.rebranded" ] || die "$fork is not a rebranded checkout; run ../fork.sh first"
[ -f "$core/patches/series" ] || die "$core has no patches/series"
[ -f "$config" ] || die "config not found: $config (set FORK_CONFIG=path/to/fork.toml)"
[ "$(uname -s)-$(uname -m)" = "Linux-x86_64" ] || die "Android builds need x86-64 Linux"

slug="$(python3 -c "import sys, tomllib; print(tomllib.load(open(sys.argv[1], 'rb'))['fork']['slug'])" "$config")"
version="$(cat "$core/chromium_version.txt")"
work="${ANDROID_WORK_DIR:-$(dirname "$fork")/$slug-android}"
src="$work/src"
out="$src/out/Default"
stamps="$work/.stamps"

done_already() { [ -f "$stamps/$1" ]; }
mark() { mkdir -p "$stamps"; touch "$stamps/$1"; }

setup_depot_tools() {
    if ! command -v gclient >/dev/null; then
        if [ ! -d "$work/depot_tools" ]; then
            say "getting depot_tools"
            mkdir -p "$work"
            git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git \
                "$work/depot_tools"
        fi
        export PATH="$PATH:$work/depot_tools"
    fi
}

# Chromium $version with the Android dependencies (SDK, NDK, Java libraries).
stage_fetch() {
    done_already fetch && return 0
    say "fetching Chromium $version for Android into $src (tens of GB)"
    mkdir -p "$work"
    if [ ! -d "$src/.git" ]; then
        # depth 2: generating LASTCHANGE needs the parent commit (same as Helium's clone.py)
        git clone -c advice.detachedHead=false --depth=2 -b "$version" \
            https://chromium.googlesource.com/chromium/src.git "$src"
    fi
    cat > "$work/.gclient" <<'EOF'
solutions = [
  {
    "name": "src",
    "url": "https://chromium.googlesource.com/chromium/src.git",
    "managed": False,
    "custom_deps": {
      # Large GPU conformance test suite, not needed to build (Helium skips it too)
      "src/third_party/angle/third_party/VK-GL-CTS/src": None,
    },
    "custom_vars": {},
  },
]
target_os = ["android"]
EOF
    (cd "$work" && gclient sync --no-history --nohooks -D)
    mark fetch
}

# System packages Chromium needs. This asks for sudo.
stage_deps() {
    done_already deps && return 0
    say "installing Chromium's build dependencies (will ask for your password)"
    # DEPS_ARGS: e.g. "--android --no-prompt" for unattended machines
    # shellcheck disable=SC2086
    (cd "$src" && ./build/install-build-deps.sh ${DEPS_ARGS:-})
    mark deps
}

stage_hooks() {
    done_already hooks && return 0
    say "running Chromium's hooks (downloads toolchains)"
    (cd "$work" && gclient runhooks)
    mark hooks
}

# Helium on top of Chromium: same steps and order as helium-linux/scripts/shared.sh,
# without the two that only make sense for the desktop source tarball
# (binary pruning) or that are best added once the build works (domain
# substitution; set DOMSUB=1 to include it).
stage_helium() {
    done_already helium && return 0
    say "downloading Helium's extra sources (search engine data, uBlock Origin)"
    mkdir -p "$work/download_cache"
    "$core/utils/downloads.py" retrieve -i "$core/deps.ini" -c "$work/download_cache"
    "$core/utils/downloads.py" unpack -i "$core/deps.ini" -c "$work/download_cache" "$src"

    say "applying Helium's patches"
    "$core/utils/patches.py" apply "$src" "$core/patches"

    say "applying Android guards"
    patch -p1 --ignore-whitespace --no-backup-if-mismatch --forward \
        -d "$src" -i "$here/patches/android-guards.patch"

    if [ "${DOMSUB:-0}" = 1 ]; then
        say "substituting Google domains"
        "$core/utils/domain_substitution.py" apply -r "$core/domain_regex.list" \
            -f "$core/domain_substitution.list" -c "$work/subs.tar.gz" "$src"
    fi

    say "renaming Chromium's strings"
    python3 "$core/utils/name_substitution.py" --sub -t "$src" \
        --backup-path "$work/namesubs.tar"

    say "applying translations"
    python3 "$core/utils/i18n_apply.py" -t "$src"

    say "setting the version"
    [ -f "$work/revision.txt" ] || echo 1 > "$work/revision.txt"
    python3 "$core/utils/helium_version.py" --tree "$core" --platform-tree "$work" \
        --chromium-tree "$src"

    say "installing shared branding resources"
    python3 "$core/utils/generate_resources.py" "$core/resources/generate_resources.txt" \
        "$core/resources"
    python3 "$core/utils/replace_resources.py" "$core/resources/helium_resources.txt" \
        "$core/resources" "$src"
    mark helium
}

stage_brand() {
    done_already brand && return 0
    say "applying Android branding"
    python3 "$here/brand_android.py" --config "$config" --src "$src"
    mark brand
}

stage_gn() {
    say "writing build arguments"
    mkdir -p "$out"
    {
        echo "# Helium's flags.gn"
        cat "$core/flags.gn"
        echo
        python3 "$here/brand_android.py" --config "$config" --print-args \
            --profile "${GN_PROFILE:-release}"
    } > "$out/args.gn"
    cat "$out/args.gn"
    (cd "$src" && gn gen out/Default)
}

stage_build() {
    say "compiling (hours)"
    # BUILD_TIMEOUT: seconds before stopping cleanly (exit 124) so a later run can
    # resume. NINJA_ARGS: extra flags, e.g. "-k 0" to collect every error in one pass.
    # shellcheck disable=SC2086
    if [ -n "${BUILD_TIMEOUT:-}" ]; then
        (cd "$src" && timeout -k 3m -s INT "$BUILD_TIMEOUT" \
            autoninja ${NINJA_ARGS:-} -C out/Default chrome_public_apk)
    else
        (cd "$src" && autoninja ${NINJA_ARGS:-} -C out/Default chrome_public_apk)
    fi
    echo
    echo "Built: $out/apks/ChromePublic.apk"
    echo "Install on a phone connected over USB with debugging enabled:"
    echo "    (cd \"$src\" && out/Default/bin/chrome_public_apk install)"
}

# Re-run the static triage against your patched tree.
stage_triage() {
    python3 "$here/triage.py" --chromium "$src" --helium "$core" --out "$work/triage"
}

case "$stage" in
    all)
        setup_depot_tools
        stage_fetch; stage_deps; stage_hooks; stage_helium; stage_brand; stage_gn; stage_build ;;
    fetch|hooks|gn|build)
        setup_depot_tools
        "stage_$stage" ;;
    deps|helium|brand|triage)
        "stage_$stage" ;;
    *) die "unknown stage: $stage" ;;
esac
