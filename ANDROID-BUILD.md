# Zade for Android (experimental build branch)

This branch builds an Android APK of Zade from the open-source
[Helium browser](https://github.com/imputnet/helium) (GPL-3.0), which is based
on Chromium. It is separate from the plan described in `README.md` on `main`.

- `kit/` turns Helium's source into Zade (name, icon, package ID) and holds
  the Android port. Start with `kit/README.md` and `kit/android/README.md`.
- `.github/workflows/android.yml` runs the build on GitHub's servers in up to
  six chained passes. Pushing to this branch starts a build.
- Build logs are published to the `android-build-logs` branch.
- A finished APK appears under the run's **Artifacts** on the Actions tab. It
  is signed with Chromium's public debug key: fine for your own phone, not for
  distribution.

Not affiliated with or endorsed by the Helium project. Keep changes here;
Helium does not accept AI-assisted contributions.
