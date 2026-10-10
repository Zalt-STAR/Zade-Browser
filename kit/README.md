# Helium fork kit

Turns the open-source [Helium browser](https://github.com/imputnet/helium) into
your own browser, with its own name, logo, binary, profile directory and
`yourname://` pages. Helium is licensed GPL-3.0, so this is a real fork of the
real source, not a reverse-engineered copy.

Tested against Helium 0.19.2.1 (Chromium 155.0.8059.39), Linux.

## 1. Name it

Edit `fork.toml`. The shipped values (`Argon`, `dev.example.argon`, `Your Name`)
are placeholders. Pick your own name, and check it is not already used by
another browser before you share builds.

## 2. Fork it

```bash
pip install --user 'pillow>=10.1'    # only dependency besides git and Python 3.11+
./fork.sh
```

This clones `helium-linux` (plus its `helium-chromium` submodule) at the tested
commits into `./<slug>-linux`, rebrands it, and commits the result on a new
branch in both repos. It takes about a minute and prints what it changed.

To preview without writing anything, on a checkout you made yourself:

```bash
python3 rebrand.py --config fork.toml --tree path/to/helium-linux --check
```

## 3. Build it

```bash
cd <slug>-linux
scripts/docker-build.sh
```

That is Helium's own build script. It builds a Debian container with the
toolchain, downloads the Chromium source tarball, applies the patches and
compiles. Expect:

- **Docker** with buildx.
- **Hours** of compile time on a desktop CPU. Chromium's own guidance is 16 GB
  of RAM or more and about 100 GB of free disk.
- The binary at `build/src/out/Default/<slug>`.

Run it straight from there:

```bash
build/src/out/Default/<slug>
```

Package it (`.tar.xz` and AppImage in `build/release/`; add `MAKE_DEB=1` for a `.deb`):

```bash
package/docker-package.sh
```

## What the rebrand changes

| Area | Before | After |
| --- | --- | --- |
| Product name everywhere in the UI, 81 languages | Helium | your `name` |
| Built-in pages | `helium://settings` | `<slug>://settings` |
| Binary, package, `.desktop`, icon name | `helium` | `<slug>` |
| Profile directory | `~/.config/net.imput.helium` | `~/.config/<rdn>` |
| Logo | Helium's | generated placeholder (three circles on a dark tile) |
| Crash reports | Helium's crash server | off (or your `crash_url`) |
| Update feed (macOS/Windows) | Helium's | `update_url` (a dead address by default) |
| AppImage self-update | Helium's GitHub releases | off (or your `github_repo`) |
| Bug-report link, maintainer details | imput | yours |

Your browser and a real Helium install can live side by side: they share
nothing on disk.

## What it leaves alone, on purpose

- **Copyright headers and the credits entry for Helium.** The GPL requires
  them. The kit also writes `FORK_NOTICE.md` stating that this is a modified
  Helium, with the date, which the licence asks for too.
- **Internal identifiers** such as `helium::` namespaces, `HELIUM_*` macros,
  source file paths and preference keys. Users never see them, and keeping them
  means newer Helium releases still merge cleanly.
- **Helium services** (`services_origin`). Extension installs, uBlock filter
  lists, spellcheck dictionaries and `!bangs` go through Helium's anonymising
  proxy. That is fine for a build you use yourself. If you give builds to other
  people, run your own instance
  ([helium-services](https://github.com/imputnet/helium-services)) and set its
  URL in `fork.toml`, rather than sending your users' traffic to their servers.
- **Upstream CI and signing key** (`.github/`, `pubkey.asc`). Delete or replace
  these before pushing the repo anywhere; they belong to Helium's release
  process, not yours.

## Your own logo

Overwrite the files in `helium-chromium/resources/branding/` (same names, same
sizes), or change the colours under `[logo]` in `fork.toml` and re-run.
`make_branding.py` shows exactly which files and sizes are expected; the two
`.icon` files are Chromium's vector format (a list of path commands).

## Publishing

If you only use the build yourself, you have no obligations. If you give a
build to anyone else, GPL-3.0 applies:

1. Publish the source: push both repos (`<slug>-linux` and its
   `helium-chromium` tree) somewhere public, and point the submodule URL in
   `.gitmodules` at your copy of `helium-chromium`.
2. Keep it under GPL-3.0, and keep the licence files and notices.
3. Do not call it Helium or use Helium's logo. The rebrand handles this.

Keep your changes in your own repositories. Helium does not accept AI-assisted
contributions, so do not open pull requests against `imputnet` repos with
anything produced by this kit.

## Updating to a newer Helium

Bump `linux_commit` in `fork.sh` and run it again in an empty directory. Every
targeted edit in `rebrand.py` checks that it matched; if Helium moved something,
the script stops and names the edit to fix instead of producing a half-renamed
tree.

## Android

`android/` holds an Android port: a patch-by-patch triage, Android branding and
a build script. It is prepared but not compiled; read `android/README.md`
before starting, because it carries less of Helium over than the desktop build
does.

## Other platforms

The core rebrand (name, scheme, logo, endpoints, translations) lives in
`helium-chromium` and applies everywhere. Binary and package naming is done
for Linux only. `helium-macos` and `helium-windows` need the same treatment in
their own packaging files.

## How this was checked

The full compile was not run (it needs the machine described above). What was
checked, against the real Chromium 155.0.8059.39 files that Helium's patches
touch:

- All 355 patches were applied twice, once original and once rebranded. Both
  runs gave the same result patch for patch, and the two resulting trees differ
  only in the intended lines (141 renamed lines plus 36 targeted edits across
  58 files).
- Helium's name-substitution step ran on both trees and rewrote the same 76
  string files. No "Helium" or `helium://` is left in the rebranded UI strings.
- All 22,842 translated strings still match their source string after the
  rename.

Twelve patches could not be exercised in either run because they modify
bundled third-party code (DevTools, uBlock Origin, BoringSSL, search-engine
data) that ships in the full source tarball but not in Chromium's main
repository. The rebrand changes only added lines in those, plus four context
lines that earlier patches create, so they should behave as upstream does; the
first real build is the confirmation.
