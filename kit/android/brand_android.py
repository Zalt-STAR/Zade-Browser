#!/usr/bin/env python3
"""
Android-side branding for the fork: app name, launcher icons and GN arguments.

Helium's own rebrand machinery (patches, name substitution, translations)
already renames the browser inside the engine and in Chromium's strings. This
covers what only exists on Android: the app label, the launcher icon set and
the package ID.

  brand_android.py --config fork.toml --src path/to/chromium/src [--check]
  brand_android.py --config fork.toml --print-args      # GN arguments only

Written against Chromium 155.0.8059.39. Each edit checks that it matched.
"""

import argparse
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import make_branding  # noqa: E402  pylint: disable=wrong-import-position
import rebrand  # noqa: E402  pylint: disable=wrong-import-position
from PIL import Image, ImageDraw  # noqa: E402  pylint: disable=wrong-import-position

RES = 'chrome/android/java/res_chromium_base'
DENSITIES = {'mdpi': 1.0, 'hdpi': 1.5, 'xhdpi': 2.0, 'xxhdpi': 3.0, 'xxxhdpi': 4.0}
LEGACY_DP = 48       # app_icon.png
ADAPTIVE_DP = 108    # layered_app_icon*.png
# Launchers crop adaptive icons to the middle 66 of 108 dp; keep the mark inside it.
SAFE_MARGIN = 0.29
CPUS = ('arm64', 'arm', 'x64', 'x86')


def load_android(path):
    """Reads the optional [android] table of fork.toml."""
    with open(path, 'rb') as file:
        table = tomllib.load(file).get('android', {})
    settings = {
        'package': table.get('package', ''),
        'target_cpu': table.get('target_cpu', 'arm64'),
        'keystore_path': table.get('keystore_path', ''),
        'keystore_name': table.get('keystore_name', ''),
        'keystore_password': table.get('keystore_password', ''),
    }
    if settings['target_cpu'] not in CPUS:
        raise rebrand.RebrandError(f'[android] target_cpu must be one of {", ".join(CPUS)}')
    keystore = [settings[k] for k in ('keystore_path', 'keystore_name', 'keystore_password')]
    if any(keystore) and not all(keystore):
        raise rebrand.RebrandError('[android] set keystore_path, keystore_name and '
                                   'keystore_password together, or none of them')
    if any('"' in value or '\\' in value or '$' in value for value in keystore):
        raise rebrand.RebrandError('[android] keystore values: no quotes, backslashes or $')
    return settings


def gn_args(config, android, profile='release'):
    """GN arguments for an APK, to append after Helium's flags.gn.

    `release` is a full official build. `ci` trades optimisation for a build
    that fits a 4-core, 16 GB machine: no link-time optimisation, no debug
    symbols, compiler cache on, one link at a time.
    """
    package = android['package'] or config.rdn
    lines = [
        '# Android target',
        'target_os = "android"',
        f'target_cpu = "{android["target_cpu"]}"',
        'is_debug = false',
        'is_component_build = false',
        'blink_symbol_level = 0',
        'ffmpeg_branding = "Chrome"',
        'proprietary_codecs = true',
    ]
    if profile == 'ci':
        lines += [
            '',
            '# Small-machine profile',
            'is_official_build = false',
            'dcheck_always_on = false',
            'debuggable_apks = false',
            'android_static_analysis = "off"',
            'symbol_level = 0',
            'v8_symbol_level = 0',
            'cc_wrapper = "ccache"',
            # Clang header modules make nearly every compile uncacheable for ccache
            # (first CI pass: 84% of compiler calls). Chromium already turns them
            # off for other compiler wrappers; do the same here.
            'use_clang_modules = false',
            'concurrent_links = 1',
            '',
            '# Identity: installs beside Chrome and Chromium instead of replacing them',
            f'chrome_public_manifest_package = "{package}"',
        ]
    else:
        lines += [
            'is_official_build = true',
            'symbol_level = 1',
            '',
            '# Identity: installs beside Chrome and Chromium instead of replacing them',
            'android_channel = "stable"',
            f'chrome_public_manifest_package = "{package}"',
        ]
    if android['keystore_path']:
        lines += [
            '',
            '# Release signing key',
            f'android_keystore_path = "{android["keystore_path"]}"',
            f'android_keystore_name = "{android["keystore_name"]}"',
            f'android_keystore_password = "{android["keystore_password"]}"',
        ]
    else:
        lines += [
            '',
            '# No signing key configured: the APK is signed with Chromium\'s public',
            '# debug key. Fine on your own phone; never distribute a build signed with it.',
        ]
    return '\n'.join(lines) + '\n'


def legacy_icon(size, config):
    """Pre-Android-8 launcher icon: the rounded tile."""
    return make_branding.tile_icon(size, config.tile, config.accent, inset_ratio=0.04)


def adaptive_background(size, config):
    """Adaptive icon background layer: solid tile colour, fully opaque."""
    return Image.new('RGBA', (size, size), config.tile)


def adaptive_foreground(size, config):
    """Adaptive icon foreground layer: the mark alone, inside the safe zone."""
    scale = make_branding.SS
    big = size * scale
    img = Image.new('RGBA', (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {'accent': config.accent, 'light': make_branding.LIGHT}
    make_branding.draw_circles(draw, make_branding.fitted_mark(big, big * SAFE_MARGIN), colors)
    return img.resize((size, size), Image.LANCZOS)


def themed_icon_xml():
    """Monochrome layer for Android 13 themed icons, as a vector drawable."""
    paths = []
    # Upstream draws inside the 27..63 box of a 90 x 90 viewport; do the same.
    for cx, cy, radius, _ in make_branding.fitted_mark(36, 0):
        cx, cy = cx + 27, cy + 27
        data = (f'M{cx - radius:.2f},{cy:.2f}'
                f'a{radius:.2f},{radius:.2f} 0 1,0 {2 * radius:.2f},0'
                f'a{radius:.2f},{radius:.2f} 0 1,0 {-2 * radius:.2f},0Z')
        paths.append(f'    <path\n        android:pathData="{data}"\n'
                     '        android:fillColor="#000000"/>')
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<!-- Placeholder branding generated by brand_android.py. -->\n'
            '<vector xmlns:android="http://schemas.android.com/apk/res/android"\n'
            '    android:width="90dp"\n    android:height="90dp"\n'
            '    android:viewportWidth="90"\n    android:viewportHeight="90">\n'
            + '\n'.join(paths) + '\n</vector>\n')


def expected_size(path):
    """Pixel size of an existing PNG, so replacements match upstream exactly."""
    with Image.open(path) as image:
        return image.size


def brand(config, src, check):
    """Applies the Android branding to a Chromium checkout."""
    tree = rebrand.Tree(src, check)
    if not (tree.root / RES).is_dir():
        raise rebrand.RebrandError(
            f'{tree.root / RES} not found. Is this a Chromium checkout with Android enabled?')

    strings = f'{RES}/values/channel_constants.xml'
    text = tree.read(strings)
    # Runs after Helium's name substitution in a full build, so accept either spelling.
    current = next((name for name in ('Chromium', config.name)
                    if f'<string name="app_name" translatable="false">{name}</string>' in text),
                   None)
    if current is None:
        raise rebrand.RebrandError(f'{strings}: app_name is not where it is expected')
    if current != config.name:
        for old in ('">Chromium</string>', '">Chromium bookmarks</string>',
                    '">Chromium search</string>', '">Chromium quick action search</string>'):
            tree.exact(strings, old, old.replace('Chromium', config.name))
    tree.note(f'app name: {config.name} (launcher label and widget titles)')

    written = 0
    for density, factor in DENSITIES.items():
        folder = tree.root / RES / f'mipmap-{density}'
        jobs = {
            'app_icon.png': (int(LEGACY_DP * factor), legacy_icon),
            'layered_app_icon.png': (int(ADAPTIVE_DP * factor), adaptive_foreground),
            'layered_app_icon_background.png': (int(ADAPTIVE_DP * factor), adaptive_background),
        }
        for name, (size, draw) in jobs.items():
            target = folder / name
            if not target.exists():
                raise rebrand.RebrandError(f'{target}: launcher icon missing upstream')
            if expected_size(target) != (size, size):
                raise rebrand.RebrandError(
                    f'{target}: upstream size {expected_size(target)} != expected {size}px')
            if not check:
                draw(size, config).save(target)
            written += 1

    themed = f'{RES}/drawable/themed_app_icon.xml'
    if 'android:viewportWidth="90"' not in tree.read(themed):
        raise rebrand.RebrandError(f'{themed}: viewport changed upstream')
    tree.write(themed, themed_icon_xml())
    tree.note(f'launcher icons: {written} PNGs across {len(DENSITIES)} densities, '
              'plus the themed (monochrome) icon')
    return tree


def main():
    """CLI entrypoint"""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--src', type=Path, help='Chromium src directory')
    parser.add_argument('--check', action='store_true', help='report without writing')
    parser.add_argument('--print-args', action='store_true', help='print GN arguments and exit')
    parser.add_argument('--profile', choices=('release', 'ci'), default='release',
                        help='argument profile for --print-args (ci = small build machine)')
    args = parser.parse_args()

    try:
        config = rebrand.load_config(args.config)
        android = load_android(args.config)
        if args.print_args:
            sys.stdout.write(gn_args(config, android, args.profile))
            return 0
        if not args.src:
            parser.error('--src is required unless --print-args is given')
        tree = brand(config, args.src, args.check)
    except rebrand.RebrandError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1

    print(('Would brand' if args.check else 'Branded') + f' {args.src} for Android')
    for line in tree.log:
        print(f'  - {line}')
    print(f'  - package ID: {android["package"] or config.rdn}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
