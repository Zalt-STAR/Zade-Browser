# Android port

An Android build of your fork: Helium's engine-level changes on Chromium for
Android, with your name, icon and package ID.

**Status: prepared, not compiled.** Everything here was checked against the
real Chromium 155.0.8059.39 source files it touches, but nobody has run the
compile. Expect the first builds to stop with errors. This folder is built to
make those rounds short: it tells you where the errors will be and why.

## What you get, and what you don't

| | On Android |
| --- | --- |
| Your name, icon, package ID, `<slug>://` pages | yes |
| De-Googling, privacy defaults, fingerprint noise, network changes | yes, where the code is shared (240 of 346 patches change the Android build) |
| Helium's look: tab layouts, toolbar, settings pages, onboarding | no. That is desktop UI code; Android's interface is separate Java code that Helium never touched |
| uBlock Origin | no. Helium ships it as an extension, and Android Chromium does not run extensions |
| Settings switches for Helium features | mostly no. The switches live in the desktop settings page; on Android the features run with their defaults |

So the result is: Chromium for Android's interface, your branding, Helium's
engine behaviour underneath. Ad blocking needs a different approach on Android
(for example DNS-level blocking on the phone).

## Files

| File | What it does |
| --- | --- |
| `build.sh` | Fetches Chromium for Android, applies Helium and your branding, compiles |
| `brand_android.py` | App name, launcher icons, package ID, signing settings |
| `patches/` (applied in order) | Fixes the five places where Helium's patches break an Android build |
| `triage.py` | Works out, patch by patch, what applies to Android |
| `TRIAGE.md`, `triage.csv` | Its results for this version |

## Steps

You need an x86-64 Linux machine (Ubuntu is what Chromium supports), roughly
100 GB of free disk and 16 GB or more of RAM. Chromium cannot be built for
Android on macOS or Windows.

1. Run `../fork.sh` first if you have not. It produces `<slug>-linux`.
2. Fill in `[android]` in `../fork.toml`.
3. Run:

   ```bash
   android/build.sh <slug>-linux
   ```

   It works in stages (`fetch`, `deps`, `hooks`, `helium`, `brand`, `gn`,
   `build`) and remembers which are done, so after fixing an error you re-run
   the same command, or one stage: `android/build.sh <slug>-linux build`.
   The `deps` stage runs Chromium's own dependency installer, which asks for
   your password.
4. Install on a phone with USB debugging on:

   ```bash
   cd <slug>-android/src && out/Default/bin/chrome_public_apk install
   ```

## Where it will break first

**1. Safe Browsing (near certain).** Helium compiles Safe Browsing out
(`safe_browsing_mode=0` in its `flags.gn`) and its patches remove the desktop
code that used it. Android's app, though, depends on Safe Browsing Java code
unconditionally (`//chrome/browser/safe_browsing/android:java`,
`//components/safe_browsing/android:safe_browsing_java`). Expect compile errors
in that area. Two ways through:

- Keep Helium's setting and stub out the Android callers one error at a time.
  More work; keeps Helium's privacy posture.
- Add `safe_browsing_mode = 2` (Android's default) at the end of
  `out/Default/args.gn` and re-run the `build` stage. Less work if it
  compiles, but it re-enables a Google-backed feature that Helium deliberately
  removes, and Helium's removals may then leave gaps of their own.

**2. Java callers of removed C++ features.** Same pattern as above for
sign-in, sync and Google Cloud Messaging: the patches were written for
desktop, and Android has Java code calling into those features that no patch
here adjusts. `triage.py` cannot see Java.

**3. Dependencies on desktop-only code (handled).** Five patches made code
that is compiled on Android depend on code that is not. The details are in
`TRIAGE.md`; `patches/` (applied in order) wraps each one, and with it applied
the triage finds none left. The guard patch applies cleanly but is itself
uncompiled.

**4. uBlock resources.** One patch adds uBlock's resource bundle to the shared
resource list (`chrome/chrome_paks.gni`). If that target fails on Android,
exclude it there with `if (!is_android)`.

When a build stops, the fix is almost always one of two shapes: wrap the
desktop-only part in `#if !BUILDFLAG(IS_ANDROID)` (C++) or `if (!is_android)`
(GN), or give the Android caller a stub. Send me the first error block and
I'll write the patch.

## What was checked

- **Triage:** every file touched by the 346 patches was classified from
  Chromium's roughly 8,000 build files, and spot-checked against files whose answer is
  known. Result: 240 patches change the Android build, 88 are inert there, 18
  could not be decided.
- **Guard patch:** applies without rejects to the fully patched tree, and
  re-running the triage afterwards reports no remaining desktop dependencies.
- **Branding:** run against Chromium's real Android resources. All 15 launcher
  PNGs match upstream's sizes, the app name strings were found and replaced,
  and Helium's name-substitution step renames Android's own string file too
  (247 mentions).
- **Build script:** syntax-checked, and its `brand` stage was run for real.
  The `fetch`, `deps`, `hooks`, `gn` and `build` stages follow Chromium's
  Android build guide but were not run.

## Signing and distribution

With no key configured, the APK is signed with Chromium's public debug key.
That is fine on your own phone and must never be distributed: anyone can sign
an "update" with that key. To share builds, create a key with `keytool`, set
the three `keystore_*` values in `fork.toml`, and keep the key safe; Android
ties every future update to it. The GPL obligations in the main README apply
to APKs as well.
