#!/usr/bin/env bash
# Clones Helium's Linux repo at the commits this kit was tested against,
# rebrands it using fork.toml, and commits the result on a new local branch.
#
# usage: ./fork.sh [path/to/fork.toml]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
config="$(realpath "${1:-$here/fork.toml}")"

# helium-linux commit; its submodule pins the matching helium commit (9aa23f3).
# Helium 0.19.2.1, Chromium 155.0.8059.39.
linux_commit="52ad0e4b3c9716800fd7c6fcbf5aa3500a2d884e"

die() { echo "error: $*" >&2; exit 1; }

command -v git >/dev/null || die "git is not installed"
command -v python3 >/dev/null || die "python3 is not installed"
python3 - <<'EOF' || die "need Python 3.11+ and Pillow 10.1+ (pip install --user 'pillow>=10.1')"
import sys
assert sys.version_info >= (3, 11)
import PIL
assert tuple(int(p) for p in PIL.__version__.split('.')[:2]) >= (10, 1)
EOF

read_config() {
    python3 -c "import sys, tomllib; print(tomllib.load(open(sys.argv[1], 'rb'))['fork'][sys.argv[2]])" \
        "$config" "$1"
}

name="$(read_config name)"
slug="$(read_config slug)"
dest="$PWD/$slug-linux"

[ -e "$dest" ] && die "$dest already exists; move it away or run from another directory"

echo "==> cloning helium-linux into $dest"
git clone https://github.com/imputnet/helium-linux.git "$dest"
git -C "$dest" checkout --quiet "$linux_commit"
git -C "$dest" submodule update --init

echo "==> rebranding as $name"
python3 "$here/rebrand.py" --config "$config" --tree "$dest"

# Keep the rebrand as one commit per repo, so the changes are easy to
# review, publish and rebase onto newer Helium releases later.
commit() {
    local repo="$1"
    local -a identity=()
    if ! git -C "$repo" config user.email >/dev/null; then
        identity=(-c "user.name=$(read_config developer)" -c "user.email=$(read_config email)")
    fi
    git -C "$repo" checkout --quiet -b "$slug"
    git -C "$repo" add -A
    git -C "$repo" "${identity[@]}" commit --quiet -m "Rebrand Helium as $name"
}
commit "$dest/helium-chromium"
commit "$dest"

cat <<EOF

Done. Your fork is in $dest (branch "$slug" in both repos).

Build it (needs Docker; takes hours and a lot of disk):
    cd "$dest" && scripts/docker-build.sh

Then run it:
    "$dest/build/src/out/Default/$slug"

See README.md in the kit for packaging and publishing.
EOF
