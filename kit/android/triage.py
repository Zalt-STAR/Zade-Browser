#!/usr/bin/env python3
"""
Sorts Helium's patches by what they mean for an Android build.

For every file a patch touches, it works out from Chromium's own build
definitions (BUILD.gn / .gni) whether that file is compiled when
target_os = "android", then flags patches where shared code starts
depending on desktop-only code, which is what breaks an Android compile.

It reads; it changes nothing. Run it against a Chromium tree that already has
the patches applied, so files Helium adds are classified too:

  triage.py --chromium path/to/src --helium path/to/helium-chromium --out DIR

This is static analysis. It predicts where the compiler will complain; it
does not replace compiling.
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# What GN variables look like for chrome_public_apk on a phone. Anything not
# listed is treated as unknown rather than guessed.
ANDROID_ENV = {
    'is_android': True, 'is_posix': True, 'is_clang': True, 'use_blink': True,
    'is_official_build': True, 'enable_widevine': True,
    'is_win': False, 'is_mac': False, 'is_linux': False, 'is_chromeos': False,
    'is_ios': False, 'is_fuchsia': False, 'is_apple': False, 'is_castos': False,
    'is_cast_android': False, 'is_desktop_android': False, 'is_debug': False,
    'is_component_build': False, 'toolkit_views': False, 'use_aura': False,
    'use_ozone': False, 'enable_extensions': False, 'enable_extensions_core': False,
    'enable_desktop_android_extensions': False, 'enable_dice_support': False,
    'enable_session_service': False, 'enable_background_mode': False,
    'enable_plugins': False, 'enable_ppapi': False, 'use_dbus': False,
    'use_udev': False, 'use_gtk': False, 'use_qt5': False, 'use_qt6': False,
    'enable_webui_tab_strip': False, 'enable_mdns': False, 'enable_remoting': False,
    'enable_hangout_services_extension': False, 'is_chrome_branded': False,
    'is_chrome_for_testing': False, 'is_chrome_for_testing_branded': False,
    'enable_service_discovery': False, 'enable_reporting': False,
    'safe_browsing_mode': 0, 'target_os': 'android', 'current_os': 'android',
    'host_os': 'linux',
}

SOURCE_EXT = ('.cc', '.h', '.mm', '.m', '.c', '.cpp', '.java', '.mojom', '.ts', '.js',
              '.html', '.css', '.grd', '.grdp', '.json', '.icon', '.png', '.svg', '.py',
              '.proto', '.idl', '.xml', '.rs', '.gni', '.pak', '.json5', '.tmpl')

DESKTOP_PATHS = re.compile(
    r'^(chrome/browser/ui/views/|ui/views/|chrome/browser/resources/|chrome/installer/|'
    r'chrome/app/theme/|chrome/updater/|third_party/ublock/|third_party/devtools-frontend/|'
    r'chrome/browser/ui/webui/settings/|chrome/browser/ui/webui/side_panel/|'
    r'chrome/browser/ui/tabs/|chrome/browser/ui/toolbar/|chrome/browser/ui/startup/|'
    r'chrome/browser/ui/cocoa/|chrome/browser/shell_integration|chrome/browser/first_run/|'
    r'chrome/utility/importer/|chrome/browser/importer/|chrome/common/importer/|'
    r'chrome/browser/themes/|chrome/app/vector_icons/|chrome/enterprise_companion/|'
    r'chrome/browser/web_applications/|chrome/browser/apps/|chrome/browser/media/router/|'
    r'chrome/browser/devtools/|chrome/browser/ui/browser_|chrome/test/)')
DESKTOP_SUFFIX = re.compile(r'(_mac|_win|_linux|_chromeos|_views|_aura|_desktop|_cocoa)'
                            r'(_unittest|_browsertest)?\.[a-z]+$|\.mm$')
ANDROID_HINT = re.compile(r'(^|/)android/|_android\.|\.java$')
GUARD_WORDS = re.compile(r'IS_ANDROID|ENABLE_EXTENSIONS|TOOLKIT_VIEWS|IS_WIN|IS_MAC|IS_LINUX|'
                         r'IS_CHROMEOS|ENABLE_DICE|ENABLE_SESSION_SERVICE|USE_AURA|IS_OZONE')

TOKEN = re.compile(r'\s*(&&|\|\||==|!=|>=|<=|[()!<>]|"[^"]*"|[A-Za-z_][A-Za-z0-9_.]*|\d+)')


def tri_not(value):
    """Three-valued NOT (None means unknown)."""
    return None if value is None else not value


def tri_and(left, right):
    """Three-valued AND."""
    if left is False or right is False:
        return False
    if left is None or right is None:
        return None
    return True


def tri_or(left, right):
    """Three-valued OR."""
    if left is True or right is True:
        return True
    if left is None or right is None:
        return None
    return False


class Expr:
    """Evaluates a GN condition against ANDROID_ENV with unknowns allowed."""

    def __init__(self, text):
        self.tokens = TOKEN.findall(text)
        self.pos = 0
        self.clean = ''.join(self.tokens) == re.sub(r'\s+', '', text)

    def peek(self):
        """Next token or None."""
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        """Consumes one token."""
        self.pos += 1
        return self.tokens[self.pos - 1]

    def evaluate(self):
        """True / False / None (unknown)."""
        if not self.clean or not self.tokens:
            return None
        try:
            value = self.parse_or()
        except (IndexError, ValueError):
            return None
        return value if isinstance(value, bool) or value is None else None

    def parse_or(self):
        """a || b"""
        value = self.parse_and()
        while self.peek() == '||':
            self.take()
            value = tri_or(self.as_bool(value), self.as_bool(self.parse_and()))
        return value

    def parse_and(self):
        """a && b"""
        value = self.parse_cmp()
        while self.peek() == '&&':
            self.take()
            value = tri_and(self.as_bool(value), self.as_bool(self.parse_cmp()))
        return value

    def parse_cmp(self):
        """a == b, a != b, a >= b ..."""
        left = self.parse_unary()
        if self.peek() in ('==', '!=', '>=', '<=', '<', '>'):
            operator = self.take()
            right = self.parse_unary()
            if left is None or right is None or isinstance(left, Unknown) \
                    or isinstance(right, Unknown):
                return None
            if operator == '==':
                return left == right
            if operator == '!=':
                return left != right
            if isinstance(left, int) and isinstance(right, int):
                return {'>=': left >= right, '<=': left <= right,
                        '<': left < right, '>': left > right}[operator]
            return None
        return left

    def parse_unary(self):
        """!a, (a), literals and identifiers"""
        token = self.take()
        if token == '!':
            return tri_not(self.as_bool(self.parse_unary()))
        if token == '(':
            value = self.parse_or()
            if self.take() != ')':
                raise ValueError('unbalanced')
            return value
        if token.startswith('"'):
            return token[1:-1]
        if token.isdigit():
            return int(token)
        if token in ('true', 'false'):
            return token == 'true'
        return ANDROID_ENV.get(token, Unknown())

    @staticmethod
    def as_bool(value):
        """Coerces a parsed value to three-valued bool."""
        return value if isinstance(value, bool) else None


class Unknown:  # pylint: disable=too-few-public-methods
    """A GN variable whose Android value we do not assume."""


class BuildIndex:
    """Which files and targets each BUILD.gn / .gni mentions, and under what conditions."""

    def __init__(self, root):
        self.root = Path(root)
        self.files = defaultdict(list)     # source path -> [True/False/None, ...]
        self.labels = defaultdict(list)    # (build file, label) -> [state, ...]
        self.excluded = {}                 # build file -> assert expression that fails

    def scan(self):
        """Parses every build file under the tree."""
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in ('out', '.git')]
            for name in filenames:
                if name == 'BUILD.gn' or name.endswith('.gni'):
                    path = Path(dirpath) / name
                    self._scan_file(path.relative_to(self.root).as_posix(),
                                    path.read_text(encoding='utf-8', errors='replace'))
        return self

    def _scan_file(self, rel, text):
        directory = os.path.dirname(rel)
        frames = []        # one entry per open "{": condition state, or 'block'
        chains = {}        # depth -> states of the if/else-if chain just closed
        pending = None     # state for the "{" we are about to open
        failed_assert = None
        mentions = []
        i, size = 0, len(text)
        while i < size:
            char = text[i]
            if char == '#':
                i = text.find('\n', i)
                i = size if i < 0 else i
                continue
            if char == '"':
                end = i + 1
                while end < size and text[end] != '"':
                    end += 2 if text[end] == '\\' else 1
                mentions.append((text[i + 1:end], self._state(frames)))
                i = end + 1
                continue
            if char == '{':
                frames.append(pending if pending is not None else 'block')
                pending = None
                i += 1
                continue
            if char == '}':
                closed = frames.pop() if frames else 'block'
                depth = len(frames)
                if closed != 'block':
                    chains.setdefault(depth, []).append(closed[0])
                else:
                    chains.pop(depth, None)
                i += 1
                continue
            word = re.match(r'[A-Za-z_][A-Za-z0-9_]*', text[i:])
            if not word:
                i += 1
                continue
            name = word.group(0)
            i += len(name)
            depth = len(frames)
            if name == 'if':
                condition, i = self._parens(text, i)
                state = Expr(condition).evaluate()
                previous = chains.get(depth, []) if getattr(self, '_after_else', False) else []
                for earlier in previous:
                    state = tri_and(state, tri_not(earlier))
                if not getattr(self, '_after_else', False):
                    chains.pop(depth, None)
                self._after_else = False
                pending = (Expr(condition).evaluate(), state)
            elif name == 'else':
                rest = text[i:].lstrip()
                if rest.startswith('if'):
                    self._after_else = True
                else:
                    state = True
                    for earlier in chains.get(depth, []):
                        state = tri_and(state, tri_not(earlier))
                    chains.pop(depth, None)
                    pending = (None, state)
            elif name == 'assert' and depth == 0:
                condition, i = self._parens(text, i)
                first = self._first_argument(condition)
                if Expr(first).evaluate() is False and failed_assert is None:
                    failed_assert = first.strip()
            else:
                if name not in ('else',):
                    chains.pop(depth, None)
        if failed_assert:
            self.excluded[rel] = failed_assert
        for literal, state in mentions:
            if failed_assert:
                state = False
            if '$' in literal or '{{' in literal or not literal:
                continue
            if literal.startswith('//') and ('.' not in os.path.basename(literal.split(':')[0])
                                             or ':' in literal):
                self.labels[(rel, literal)].append(state)
            elif literal.endswith(SOURCE_EXT):
                target = literal[2:] if literal.startswith('//') else \
                    os.path.normpath(os.path.join(directory, literal))
                self.files[target].append(state)

    @staticmethod
    def _state(frames):
        state = True
        for frame in frames:
            if frame != 'block':
                state = tri_and(state, frame[1])
        return state

    @staticmethod
    def _parens(text, i):
        start = text.find('(', i)
        depth, j = 0, start
        while j < len(text):
            if text[j] == '"':
                j += 1
                while j < len(text) and text[j] != '"':
                    j += 2 if text[j] == '\\' else 1
            elif text[j] == '(':
                depth += 1
            elif text[j] == ')':
                depth -= 1
                if depth == 0:
                    return text[start + 1:j], j + 1
            j += 1
        return text[start + 1:], len(text)

    @staticmethod
    def _first_argument(condition):
        depth = 0
        for index, char in enumerate(condition):
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == ',' and depth == 0:
                return condition[:index]
        return condition

    def nearest_build_file(self, path):
        """The BUILD.gn that owns a path (closest ancestor)."""
        directory = os.path.dirname(path)
        while True:
            candidate = os.path.join(directory, 'BUILD.gn') if directory else 'BUILD.gn'
            if (self.root / candidate).exists():
                return candidate
            if not directory:
                return None
            directory = os.path.dirname(directory)


class Classifier:
    """Decides whether a file is part of an Android build."""

    def __init__(self, index):
        self.index = index
        self.cache = {}

    def status(self, path):
        """('android' | 'maybe' | 'desktop', reason, strong).

        `strong` is True when the verdict comes from the build files themselves,
        False when it is inferred from the file's name or location.
        """
        if path not in self.cache:
            result = self._status(path)
            self.cache[path] = result if len(result) == 3 else (*result, True)
        return self.cache[path]

    def _status(self, path):
        index = self.index
        if path.endswith('BUILD.gn') or path.endswith('.gni') or path.endswith('.gn'):
            if path in index.excluded:
                return 'desktop', f'build file asserts {index.excluded[path]}'
            return 'android', 'build configuration'
        owner = index.nearest_build_file(path)
        if owner in index.excluded:
            return 'desktop', f'{owner} asserts {index.excluded[owner]}'
        # A directory nested inside a desktop-only one is desktop-only too, even if its
        # own build file does not repeat the assert (ui/views/..., extensions/...).
        directory = os.path.dirname(path)
        while directory:
            directory = os.path.dirname(directory)
            ancestor = os.path.join(directory, 'BUILD.gn') if directory else ''
            if ancestor in index.excluded and directory.count('/') >= 1:
                return 'desktop', f'under {directory}/, which asserts {index.excluded[ancestor]}'
        states = index.files.get(path)
        if states:
            if any(state is True for state in states):
                if ANDROID_HINT.search(path):
                    return 'android', 'Android-specific source'
                return 'android', 'listed for all platforms'
            if all(state is False for state in states):
                return 'desktop', 'listed only under non-Android conditions'
            if DESKTOP_PATHS.search(path) or DESKTOP_SUFFIX.search(path):
                return 'desktop', 'desktop-only area of the tree', False
            return 'maybe', 'listed under a condition this script cannot decide', False
        if ANDROID_HINT.search(path):
            return 'android', 'Android-specific path', False
        if DESKTOP_PATHS.search(path):
            return 'desktop', 'desktop-only area of the tree', False
        if DESKTOP_SUFFIX.search(path):
            return 'desktop', 'platform-specific file name', False
        if path.endswith(('.grd', '.grdp', '.xtb')):
            return 'android', 'shared UI strings'
        if path.endswith('.h'):
            for ext in ('.cc', '.mm'):
                sibling = path[:-2] + ext
                if sibling in index.files:
                    verdict, _, strong = self.status(sibling)
                    return verdict, f'follows {os.path.basename(sibling)}', strong
        return 'maybe', 'not listed in any build file', False


def parse_patch(text):
    """Yields (target path, is_new_file, [added lines]) for each file in a patch."""
    current = None
    previous = ''
    for line in text.split('\n'):
        if line.startswith('+++ b/'):
            current = (line[6:].strip(), previous.startswith('--- /dev/null'), [])
            yield current
        elif current and line.startswith('+') and not line.startswith('+++'):
            current[2].append(line[1:])
        previous = line


def guard_at(root, path, needle):
    """Preprocessor conditions enclosing the first line containing `needle`."""
    try:
        lines = (Path(root) / path).read_text(encoding='utf-8', errors='replace').split('\n')
    except OSError:
        return None
    stack = []
    for line in lines:
        stripped = line.strip()
        if needle in line:
            return list(stack)
        if stripped.startswith(('#if', '#ifdef', '#ifndef')):
            stack.append(stripped)
        elif stripped.startswith(('#elif', '#else')) and stack:
            stack[-1] = stack[-1] + ' / ' + stripped
        elif stripped.startswith('#endif') and stack:
            stack.pop()
    return None


def triage(chromium, helium):
    """Returns per-patch rows and the build index."""
    index = BuildIndex(chromium).scan()
    classifier = Classifier(index)
    helium = Path(helium)
    series = [line.strip() for line in (helium / 'patches/series').read_text().split('\n')
              if line.strip() and not line.startswith('#')]
    rows = []
    for name in series:
        text = (helium / 'patches' / name).read_text(encoding='utf-8', errors='replace')
        counts = Counter()
        breaks, checks = [], []
        files = list(parse_patch(text))
        for path, _, added in files:
            status, _, sure = classifier.status(path)
            counts[status] += 1
            if status == 'desktop':
                continue
            if path.endswith(('BUILD.gn', '.gni')):
                for line in added:
                    for label in re.findall(r'"(//[^"$]+)"', line):
                        target_dir = label[2:].split(':')[0]
                        owner = f'{target_dir}/BUILD.gn'
                        soft = DESKTOP_PATHS.search(target_dir + '/')
                        if owner not in index.excluded and not soft:
                            continue
                        states = index.labels.get((path, label), [None])
                        if all(state is False for state in states):
                            continue
                        if owner in index.excluded and any(state is True for state in states):
                            breaks.append(f'{path}: depends on {label}, whose build file '
                                          f'asserts `{index.excluded[owner]}`')
                        else:
                            checks.append(f'{path}: depends on {label}, which looks '
                                          'desktop-only')
                continue
            for line in added:
                match = re.match(r'\s*#include "([^"]+)"', line)
                if not match:
                    continue
                header = match.group(1)
                header_status, why, header_sure = classifier.status(header)
                if header_status != 'desktop':
                    continue
                guards = guard_at(chromium, path, f'#include "{header}"')
                if guards is not None and any(GUARD_WORDS.search(g) for g in guards):
                    continue
                message = f'{path}: includes {header} ({why}) with no platform guard'
                if status == 'android' and sure and header_sure:
                    breaks.append(message)
                else:
                    checks.append(message)
        if counts['android']:
            effect = 'android'
        elif counts['maybe']:
            effect = 'maybe'
        else:
            effect = 'none'
        touches_android = any(ANDROID_HINT.search(path) for path, _, _ in files)
        rows.append({
            'patch': name,
            'group': '/'.join(name.split('/')[:2]) if name.startswith('helium/')
                     else name.split('/')[0],
            'effect': effect,
            'files': len(files),
            'android_files': counts['android'],
            'maybe_files': counts['maybe'],
            'desktop_files': counts['desktop'],
            'already_handles_android': 'yes' if touches_android else '',
            'flags': sorted(set(breaks)),
            'checks': sorted(set(checks)),
        })
    return rows, index


def write_reports(rows, out_dir):
    """Writes triage.csv (every patch) and TRIAGE.md (summary and predictions)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'triage.csv', 'w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(['patch', 'group', 'effect_on_android', 'files', 'compiled_on_android',
                         'undetermined', 'desktop_only', 'already_handles_android',
                         'expected_breaks', 'worth_checking'])
        for row in rows:
            writer.writerow([row['patch'], row['group'], row['effect'], row['files'],
                             row['android_files'], row['maybe_files'], row['desktop_files'],
                             row['already_handles_android'], ' | '.join(row['flags']),
                             ' | '.join(row['checks'])])

    by_group = defaultdict(Counter)
    for row in rows:
        by_group[row['group']][row['effect']] += 1
        by_group[row['group']]['flagged'] += bool(row['flags'])
    total = Counter(row['effect'] for row in rows)
    flagged = [row for row in rows if row['flags']]
    to_check = [row for row in rows if row['checks'] and not row['flags']]

    lines = [
        '# Android triage of Helium\'s patches', '',
        f'{len(rows)} patches, sorted by whether the files they change are compiled '
        'when `target_os = "android"`. Generated by `triage.py` from Chromium\'s own '
        'build definitions; see `triage.csv` for every patch.', '',
        '| | Patches | Meaning |', '| --- | --- | --- |',
        f'| Changes the Android build | {total["android"]} | at least one changed file is '
        'compiled for Android |',
        f'| Undetermined | {total["maybe"]} | changed files sit behind build conditions the '
        'script cannot decide |',
        f'| No effect on Android | {total["none"]} | every changed file is desktop-only, so '
        'the patch is inert there |',
        f'| Expected to break the Android build | {len(flagged)} | code that is compiled '
        'for Android gains a dependency on code that is not |',
        f'| Worth checking | {len(to_check)} | same pattern, weaker evidence |', '',
        'Keep every patch in the series, including the inert ones: later patches are '
        'written against the result of earlier ones, so dropping any makes others fail '
        'to apply. A patch to a file that is not compiled on Android costs nothing.', '',
        '## By group', '',
        '| Group | Changes Android | Undetermined | No effect | Expected breaks |',
        '| --- | --- | --- | --- | --- |',
    ]
    for group in sorted(by_group):
        counts = by_group[group]
        lines.append(f'| {group} | {counts["android"]} | {counts["maybe"]} | '
                     f'{counts["none"]} | {counts["flagged"]} |')
    lines += ['', '## Expected breaks', '',
              'Each of these needs an Android guard (`#if !BUILDFLAG(IS_ANDROID)` in C++, '
              '`if (!is_android)` in GN) around the desktop-only part. A build-file '
              'dependency fails at `gn gen`; an include fails at compile or link time.', '']
    if not flagged:
        lines.append('None found by static analysis.')
    for row in flagged:
        lines.append(f'**{row["patch"]}**')
        lines += [f'- {flag}' for flag in row['flags'] + row['checks']]
        lines.append('')
    lines += ['## Worth checking', '']
    if not to_check:
        lines.append('Nothing further.')
    for row in to_check:
        lines.append(f'**{row["patch"]}**')
        lines += [f'- {flag}' for flag in row['checks']]
        lines.append('')
    lines += [
        '## What this cannot see', '',
        '- Java and JNI code. Android\'s browser UI is Java, and several patches remove '
        'C++ features (Safe Browsing, sign-in, sync) that Java code calls into. Those '
        'failures only show up when compiling.',
        '- Whether a target is reachable. A file can be listed for all platforms in a '
        'target that only desktop builds depend on.',
        '- Behaviour. A patch that compiles on Android may still do nothing there, for '
        'example a setting whose only switch lives in the desktop settings page.', '',
    ]
    (out_dir / 'TRIAGE.md').write_text('\n'.join(lines), encoding='utf-8')
    return total, flagged


def main():
    """CLI entrypoint"""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--chromium', type=Path, required=True,
                        help='Chromium src with the patches applied')
    parser.add_argument('--helium', type=Path, required=True,
                        help='helium-chromium directory (contains patches/series)')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    rows, _ = triage(args.chromium, args.helium)
    total, flagged = write_reports(rows, args.out)
    print(f'{len(rows)} patches: {total["android"]} change the Android build, '
          f'{total["maybe"]} undetermined, {total["none"]} inert on Android, '
          f'{len(flagged)} expected to break the build')
    print(f'wrote {args.out}/TRIAGE.md and {args.out}/triage.csv')


if __name__ == '__main__':
    sys.exit(main())
