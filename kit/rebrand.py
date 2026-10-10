#!/usr/bin/env python3
"""
Rebrands a helium-linux checkout (with its helium-chromium submodule) as your
own browser, so the build no longer calls itself Helium, uses Helium's logo,
shares Helium's profile directory, or reports crashes to Helium's servers.

What changes
  * the product name in Helium's patches, translations and name-substitution rules
  * the internal URL scheme (helium://settings -> <slug>://settings)
  * binary, package, .desktop file, icon name and profile directory (Linux)
  * maintainer details, crash/update endpoints, bug-report link
  * the logo (a generated placeholder; see make_branding.py)

What deliberately stays
  * copyright headers and the credits entry for Helium (the GPL requires them)
  * internal identifiers (helium:: namespaces, HELIUM_* macros, file paths,
    pref keys), so that pulling in newer upstream Helium stays practical

Written against helium-linux 52ad0e4 / helium 9aa23f3 (Helium 0.19.2.1,
Chromium 155.0.8059.39). Every targeted edit checks that it matched; if
upstream has moved, the script stops and tells you which edit to revisit.

Usage:
  rebrand.py --config fork.toml --tree path/to/helium-linux [--check]
"""

import argparse
import datetime
import json
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import make_branding

MARKER = '.rebranded'
CORE = 'helium-chromium'

# "Helium" as a standalone word. HeliumFoo, kHeliumUIScheme, IDS_HELIUM_X are left alone.
WORD = re.compile(r'(?<![A-Za-z0-9_])Helium(?![A-Za-z0-9_])')
SCHEME = re.compile(r'(?<![A-Za-z0-9_.-])helium://')

# Lines that must keep saying Helium: licence notices and attribution.
PROTECTED = [
    re.compile(r'Copyright'),
    re.compile(r'Authors'),
    re.compile(r"'opensource_project'"),
    re.compile(r'^\+Name: Helium$'),
    re.compile(r'direct component of Helium'),
]

# Tokens in packaging files that refer to upstream tooling or upstream addresses,
# not to the product. Addresses are replaced by targeted edits afterwards.
KEEP_TOKENS = ['helium-chromium', 'helium_version.py', 'imputnet/helium-linux',
               'imputnet|helium-linux', 'helium@imput.net', 'net.imput.helium']
UPSTREAM_ADDRESSES = ['imputnet', 'imput.net', 'net.imput', 'helium.computer']


class RebrandError(Exception):
    """A targeted edit did not match; upstream has probably changed."""


@dataclass
class Config:
    """Values read from fork.toml."""
    name: str
    slug: str
    rdn: str
    developer: str
    email: str
    homepage: str
    bugtracker: str
    summary: str
    github_repo: str
    services_origin: str
    crash_url: str
    update_url: str
    tile: str
    accent: str

    @property
    def env(self):
        """Prefix for environment variables, e.g. ARGON."""
        return self.slug.upper()


def load_config(path):
    """Parses and validates fork.toml."""
    with open(path, 'rb') as file:
        raw = tomllib.load(file)
    fork, endpoints, logo = raw.get('fork', {}), raw.get('endpoints', {}), raw.get('logo', {})
    try:
        config = Config(
            name=fork['name'],
            slug=fork['slug'],
            rdn=fork['rdn'],
            developer=fork['developer'],
            email=fork['email'],
            homepage=fork['homepage'].rstrip('/'),
            bugtracker=fork['bugtracker'],
            summary=fork.get('summary', 'Web browser based on Helium and Chromium'),
            github_repo=fork.get('github_repo', ''),
            services_origin=endpoints.get('services_origin',
                                          'https://services.helium.imput.net').rstrip('/'),
            crash_url=endpoints.get('crash_url', ''),
            update_url=endpoints.get('update_url', 'https://updates.invalid/'),
            tile=logo.get('tile', '#14171F'),
            accent=logo.get('accent', '#6FE3B4'),
        )
    except KeyError as missing:
        raise RebrandError(f'fork.toml is missing [fork] {missing}') from missing

    problems = []
    if not re.fullmatch(r'[A-Z][A-Za-z0-9 ]{1,30}', config.name):
        problems.append('name: start with a capital letter; letters, digits and spaces only')
    if re.search(r'helium|chrom', config.name, re.I):
        problems.append('name: must not contain "Helium", "Chrome" or "Chromium"')
    if not re.fullmatch(r'[a-z][a-z0-9]{1,30}', config.slug):
        problems.append('slug: lowercase letters and digits only, starting with a letter')
    if config.slug in ('helium', 'chrome', 'chromium', 'about', 'file', 'data', 'devtools'):
        problems.append(f'slug: "{config.slug}" is reserved')
    if not re.fullmatch(r'[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*){2,}', config.rdn):
        problems.append('rdn: use reverse-DNS form such as dev.yourname.argon')
    if config.rdn.startswith('net.imput'):
        problems.append('rdn: must not live under net.imput (that is Helium\'s namespace)')
    for field in ('homepage', 'bugtracker', 'services_origin', 'update_url'):
        if not getattr(config, field).startswith('https://'):
            problems.append(f'{field}: must be an https:// URL')
    if config.crash_url and not config.crash_url.startswith('https://'):
        problems.append('crash_url: must be empty (disabled) or an https:// URL')
    if config.github_repo and not re.fullmatch(r'[\w.-]+/[\w.-]+', config.github_repo):
        problems.append('github_repo: use owner/repo, or leave empty')
    if WORD.search(config.developer):
        problems.append('developer: must not be "Helium"; this names you, not upstream')
    if any(ch in getattr(config, f) for f in ('developer', 'email', 'summary')
           for ch in '"\\\n$`'):
        problems.append('developer/email/summary: no quotes, backslashes, $ or backticks')
    if problems:
        raise RebrandError('fork.toml:\n  - ' + '\n  - '.join(problems))
    return config


class Tree:
    """File edits against a checkout, with a dry-run mode and a change log."""

    def __init__(self, root, check):
        self.root = Path(root)
        self.check = check
        self.log = []
        self.warnings = []
        self._pending = {}

    def read(self, rel):
        """Current text of a file, including edits made earlier in this run."""
        if rel in self._pending:
            return self._pending[rel]
        return (self.root / rel).read_text(encoding='utf-8')

    def write(self, rel, text):
        """Stores new text for a file (written to disk unless --check)."""
        self._pending[rel] = text
        if not self.check:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')

    def rename(self, old, new):
        """Renames a file, carrying over pending text."""
        if old in self._pending:
            self._pending[new] = self._pending.pop(old)
        if not self.check:
            (self.root / old).rename(self.root / new)
        elif new not in self._pending:
            self._pending[new] = (self.root / old).read_text(encoding='utf-8')
        self.note(f'renamed {old} -> {new}')

    def note(self, message):
        """Records a line for the final report."""
        self.log.append(message)

    def exact(self, rel, old, new, count=1):
        """Replaces a literal that must occur exactly `count` times."""
        text = self.read(rel)
        found = text.count(old)
        if found != count:
            raise RebrandError(
                f'{rel}: expected {count} occurrence(s) of {old!r}, found {found}.\n'
                'Upstream has probably changed this spot; update the edit in rebrand.py.')
        if old != new:
            self.write(rel, text.replace(old, new))

    def patch_files(self):
        """All patch files of the core and the platform repo."""
        files = sorted((self.root / CORE / 'patches').rglob('*.patch'))
        files += sorted((self.root / 'patches').rglob('*.patch'))
        return [p.relative_to(self.root).as_posix() for p in files]


def is_protected(line):
    """Whether a line carries a licence notice or attribution."""
    return any(rule.search(line) for rule in PROTECTED)


def rename_in_patches(tree, config):
    """
    Renames the product (and its URL scheme and app ID) on patch lines.

    Added lines are always eligible. Context and removed lines are only
    changed when an earlier Helium patch added that exact line, because
    otherwise they are upstream Chromium code and must keep matching it.
    """
    originals = {rel: tree.read(rel) for rel in tree.patch_files()}
    added = set()
    for text in originals.values():
        for line in text.split('\n'):
            if line.startswith('+') and not line.startswith('+++'):
                added.add(line[1:])

    def transform(body):
        body = WORD.sub(config.name, body)
        body = SCHEME.sub(f'{config.slug}://', body)
        return body.replace('net.imput.helium', config.rdn)

    changed_lines = changed_files = 0
    for rel, text in originals.items():
        out = []
        touched = False
        for line in text.split('\n'):
            eligible = (line[:1] in ('+', '-', ' ')
                        and not line.startswith(('+++', '---'))
                        and not is_protected(line))
            if eligible:
                body = transform(line[1:])
                if body != line[1:]:
                    if line[0] == '+' or line[1:] in added:
                        line = line[0] + body
                        changed_lines += 1
                        touched = True
                    else:
                        tree.warnings.append(f'{rel}: {line.strip()[:70]}')
            out.append(line)
        if touched:
            tree.write(rel, '\n'.join(out))
            changed_files += 1
    tree.note(f'product name: {changed_lines} patch lines in {changed_files} patches')


def rename_in_translations(tree, config):
    """Renames the product in Helium's own UI strings, in every language."""
    def english(text):
        return SCHEME.sub(f'{config.slug}://', WORD.sub(config.name, text))

    def translated(text):
        # Other languages attach suffixes ("Heliumin", "Helium-dienste"), so match anywhere.
        return SCHEME.sub(f'{config.slug}://', text.replace('Helium', config.name))

    def dump(rel, data):
        tree.write(rel, json.dumps(data, indent=2, ensure_ascii=False) + '\n')

    i18n = f'{CORE}/i18n'
    source = json.loads(tree.read(f'{i18n}/source.gen.json'))
    strings = 0
    for entry in source:
        before = entry['message']
        entry['message'] = english(entry['message'])
        entry['context'] = english(entry.get('context', ''))
        strings += entry['message'] != before
    dump(f'{i18n}/source.gen.json', source)

    files = sorted((tree.root / i18n / 'translations').glob('*.json'))
    total = 0
    for path in files:
        rel = path.relative_to(tree.root).as_posix()
        data = json.loads(tree.read(rel))
        for entry in data:
            before = entry['message']
            entry['source'] = english(entry['source'])
            for key in ('message', 'feminine', 'masculine'):
                if key in entry:
                    entry[key] = translated(entry[key])
            total += entry['message'] != before
        dump(rel, data)
    tree.note(f'translations: {strings} source strings, {total} translated strings '
              f'in {len(files)} languages')


def rename_in_name_substitution(tree, config):
    """Chromium's own strings say Chrome/Chromium; point that rewrite at the new name."""
    utils = f'{CORE}/utils/name_substitution_utils.py'
    tree.exact(utils, r"r'\1helium://'", rf"r'\1{config.slug}://'")
    tree.exact(utils, "r'Helium')", f"r'{config.name}')")

    sanity = f'{CORE}/utils/name_substitution.py'
    text = tree.read(sanity)
    start = text.index('def replacement_sanity')
    end = text.index('def parse_args')
    block = WORD.sub(config.name, text[start:end]).replace('helium://', f'{config.slug}://')
    if block == text[start:end]:
        raise RebrandError(f'{sanity}: sanity table not found where expected')
    tree.write(sanity, text[:start] + block + text[end:])
    tree.note('name substitution: Chrome/Chromium now become the new name')


def rename_scheme(tree, config):
    """helium:// -> <slug>:// for the built-in pages."""
    rel = f'{CORE}/patches/helium/core/override-chrome-protocol.patch'
    tree.exact(rel, 'kHeliumUIScheme[] = "helium";', f'kHeliumUIScheme[] = "{config.slug}";', 2)
    tree.exact(rel, 'replace(0, 6, u"helium")', f'replace(0, 6, u"{config.slug}")')
    tree.exact(rel, "protocol === 'helium:'", f"protocol === '{config.slug}:'")
    tree.note(f'URL scheme: {config.slug}://')


def rebrand_core_identity(tree, config):
    """Company, copyright line and Apple IDs in Chromium's BRANDING file."""
    rel = f'{CORE}/patches/helium/core/change-chromium-branding.patch'
    tree.exact(rel, '+COMPANY_FULLNAME=The Helium Authors',
               f'+COMPANY_FULLNAME={config.developer}')
    tree.exact(rel, '+COMPANY_SHORTNAME=The Helium Authors',
               f'+COMPANY_SHORTNAME={config.developer}')
    tree.exact(rel, '+COPYRIGHT=Copyright @LASTCHANGE_YEAR@ The Helium Authors.',
               f'+COPYRIGHT=Copyright @LASTCHANGE_YEAR@ {config.developer} '
               'and The Helium Authors.')
    tree.exact(rel, '+MAC_TEAM_ID=S4Q33XPHB4', '+MAC_TEAM_ID=')
    tree.note('BRANDING: company set to you; copyright line keeps crediting The Helium Authors')


def rebrand_endpoints(tree, config):
    """Stops the fork from talking to Helium's crash and update servers."""
    core = f'{CORE}/patches/helium/core'
    tree.exact(f'{core}/crash-reporting-prefs.patch',
               'return "https://crash.helium.computer/crash";',
               f'return "{config.crash_url}";')
    tree.exact(f'{core}/add-updater-preference.patch',
               '"https://updates.helium.computer/"', f'"{config.update_url}"')
    tree.exact(f'{core}/services-prefs.patch',
               'https://services.helium.imput.net', config.services_origin, 2)
    tree.exact(f'{CORE}/patches/helium/ui/crashes-webui-fixes.patch',
               "'https://github.com/imputnet/helium/issues/new'", f"'{config.bugtracker}'")
    tree.note('endpoints: crash uploads ' +
              (f'-> {config.crash_url}' if config.crash_url else 'disabled') +
              f'; updates -> {config.update_url}; services -> {config.services_origin}')


def rebrand_linux_patches(tree, config):
    """Binary name, desktop file, profile directory and package metadata."""
    rel = 'patches/helium/linux/change-chromium-branding.patch'
    tree.exact(rel, 'GetVar("HELIUM_DESKTOP")', f'GetVar("{config.env}_DESKTOP")')
    tree.exact(rel, 'GetVar("HELIUM_CONFIG_HOME")', f'GetVar("{config.env}_CONFIG_HOME")')
    tree.exact(rel, 'return "helium.desktop";', f'return "{config.slug}.desktop";')
    tree.exact(rel, '+PACKAGE="helium"', f'+PACKAGE="{config.slug}"')
    tree.exact(rel, '+MAINTNAME="imput"', f'+MAINTNAME="{config.developer}"')
    tree.exact(rel, '+MAINTMAIL="helium@imput.net"', f'+MAINTMAIL="{config.email}"')
    tree.exact(rel, '+PRODUCTURL="https://helium.computer"', f'+PRODUCTURL="{config.homepage}"')
    tree.exact(rel, '+DEVELOPER_NAME="The Helium Authors"',
               f'+DEVELOPER_NAME="{config.developer}"')
    tree.exact(rel, '+BUGTRACKERURL="https://github.com/imputnet/helium-linux/issues"',
               f'+BUGTRACKERURL="{config.bugtracker}"')
    tree.exact(rel, '+HELPURL="https://helium.computer/"', f'+HELPURL="{config.homepage}/"')
    tree.exact(rel, 'kBrowserDisplayName[] = "helium";',
               f'kBrowserDisplayName[] = "{config.slug}";')
    tree.exact(rel, '+  return "helium";', f'+  return "{config.slug}";')

    rel = 'patches/helium/linux/rename-chrome-binary.patch'
    text = tree.read(rel)
    lines = text.split('\n')
    hits = 0
    for index, line in enumerate(lines):
        if line.startswith('+') and not line.startswith('+++') and 'helium' in line:
            lines[index] = line.replace('helium', config.slug)
            hits += 1
    if hits != 12:
        raise RebrandError(f'{rel}: expected 12 binary-name lines, found {hits}')
    tree.write(rel, '\n'.join(lines))

    rel = 'patches/helium/linux/add-error-for-missing-desktop-file.patch'
    tree.exact(rel, '<ex>helium.desktop</ex>', f'<ex>{config.slug}.desktop</ex>')
    tree.note(f'linux: binary "{config.slug}", {config.slug}.desktop, '
              f'profile in ~/.config/{config.rdn}')


def swap_product_tokens(text, config):
    """Renames the product in a packaging file, leaving upstream tooling names alone."""
    for index, token in enumerate(KEEP_TOKENS):
        text = text.replace(token, f'\0KEEP{index}\0')
    lines = []
    for line in text.split('\n'):
        if not is_protected(line):
            line = WORD.sub(config.name, line).replace('helium', config.slug)
        lines.append(line)
    text = '\n'.join(lines)
    for index, token in enumerate(KEEP_TOKENS):
        text = text.replace(f'\0KEEP{index}\0', token)
    return text


def rebrand_packaging(tree, config):
    """.desktop file, wrappers, AppImage, .deb/.rpm spec and AppStream metadata."""
    releases = (f'https://github.com/{config.github_repo}' if config.github_repo
                else config.homepage)

    spec = 'package/helium-bin.spec'
    meta = 'package/net.imput.helium.metainfo.xml'
    pkg = 'scripts/package.sh'
    files = ['package/helium.desktop', 'package/helium-wrapper.sh',
             'package/helium-wrapper-appimage.sh', 'package/apparmor.cfg',
             'package/mkdeb.sh', spec, meta, pkg]

    # Product name first. Your own values go in afterwards, so they are never rewritten.
    for rel in files:
        before = tree.read(rel)
        after = swap_product_tokens(before, config)
        if after == before:
            raise RebrandError(f'{rel}: nothing to rename; file layout changed upstream')
        tree.write(rel, after)

    tree.exact(spec, 'URL:     https://github.com/imputnet/helium-linux', f'URL:     {releases}')
    tree.exact(spec, 'https://github.com/imputnet/helium-linux/releases/download',
               f'{releases}/releases/download', 2)
    tree.exact(spec, 'Packager: imput <helium@imput.net>',
               f'Packager: {config.developer} <{config.email}>')
    tree.exact(spec, 'Summary: Private, fast, and honest web browser',
               f'Summary: {config.summary}')
    tree.exact(spec, 'Private, fast, and honest web browser based on Chromium', config.summary)

    text = tree.read(meta)
    text, n_desc = re.subn(
        r'<description>.*?</description>',
        '<description>\n    <p>\n'
        f'      {config.name} is a web browser built from the open-source Helium browser,\n'
        '      which is based on Chromium.\n'
        '    </p>\n  </description>', text, flags=re.S)
    text, n_shots = re.subn(r'\n  <screenshots>.*?</screenshots>', '', text, flags=re.S)
    if (n_desc, n_shots) != (1, 1):
        raise RebrandError(f'{meta}: description/screenshots blocks not found as expected')
    tree.write(meta, text)
    tree.exact(meta, '<summary>Private, fast, and honest web browser</summary>',
               f'<summary>{config.summary}</summary>')
    tree.exact(meta, 'https://github.com/imputnet/helium-linux/issues', config.bugtracker)
    tree.exact(meta, 'https://github.com/imputnet/helium-linux', config.homepage, 2)
    tree.exact(meta, '<developer id="net.imput">',
               f'<developer id="{config.rdn.rsplit(".", 1)[0]}">')
    tree.exact(meta, '<name>imput</name>', f'<name>{config.developer}</name>')
    tree.exact(meta, 'net.imput.helium', config.rdn)

    # The AppImage must not self-update from Helium's releases.
    update_line = ('_update_info="gh-releases-zsync|imputnet|helium-linux|latest|'
                   '$_app_name-*-$_arch.AppImage.zsync"')
    if config.github_repo:
        owner, repo = config.github_repo.split('/')
        tree.exact(pkg, update_line,
                   f'_update_info="gh-releases-zsync|{owner}|{repo}|latest|'
                   '$_app_name-*-$_arch.AppImage.zsync"')
    else:
        tree.exact(pkg, update_line + '\n', '')
        tree.exact(pkg, '    -u "$_update_info" \\\n', '')

    tree.exact('package/mkdeb.sh', 'net.imput.helium', config.rdn)
    tree.exact(spec, 'net.imput.helium', config.rdn, 3)
    tree.exact('scripts/dev.sh', 'net.imput.helium.dev', f'{config.rdn}.dev')
    tree.exact('scripts/dev.sh', './out/Default/helium', f'./out/Default/{config.slug}')

    for rel in files:
        stale = [token for token in UPSTREAM_ADDRESSES if token in tree.read(rel)]
        if stale:
            raise RebrandError(f'{rel}: still references {stale} after rebranding')

    for old in ['helium.desktop', 'helium-wrapper.sh', 'helium-wrapper-appimage.sh',
                'helium-bin.spec']:
        tree.rename(f'package/{old}', f'package/{old.replace("helium", config.slug)}')
    tree.rename(meta, f'package/{config.rdn}.metainfo.xml')
    tree.note(f'packaging: {config.slug}.desktop, {config.slug}-bin package, AppImage ' +
              (f'updates from {config.github_repo}' if config.github_repo
               else 'self-update turned off'))


def install_branding(tree, config):
    """Replaces Helium's logo files with the generated placeholder."""
    target = tree.root / CORE / 'resources' / 'branding'
    expected = sorted(p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file())
    staging = tree.root / '.branding-staging'
    if staging.exists():
        shutil.rmtree(staging)
    try:
        produced = make_branding.generate(config.name, staging, config.tile, config.accent)
        if produced != expected:
            raise RebrandError('branding: upstream logo file set changed.\n'
                               f'  upstream: {expected}\n  generated: {produced}')
        if not tree.check:
            for rel in produced:
                shutil.copyfile(staging / rel, target / rel)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    tree.note(f'logo: {len(expected)} files replaced with the placeholder mark')


def write_notice(tree, config):
    """GPL-3.0 section 5(a): modified versions must say so, with a date."""
    today = datetime.date.today().isoformat()
    tree.write('FORK_NOTICE.md', f"""# {config.name}

{config.name} is a modified version of the Helium browser, rebranded on {today}.

- Upstream: https://github.com/imputnet/helium and https://github.com/imputnet/helium-linux
- Not affiliated with or endorsed by imput or the Helium project.
- Licence: GPL-3.0 for Helium's code and these modifications (see LICENSE);
  code imported from other projects keeps its own licence
  (see LICENSE.ungoogled_chromium and the browser's credits page).

## What was modified

- Product name, logo, internal URL scheme ({config.slug}://), binary and package names,
  profile directory ({config.rdn}).
- Crash-report and update endpoints no longer point at Helium's servers.
- Maintainer and bug-report details point at {config.developer}.

If you distribute builds, the GPL requires you to offer the matching source:
this repository, the modified helium-chromium tree, and your build scripts.
""")
    tree.note('FORK_NOTICE.md written (states that this is a modified Helium, with date)')


def leftovers(tree):
    """User-visible spots that still say Helium after the rebrand, for the report."""
    hits = []
    for rel in tree.patch_files():
        for line in tree.read(rel).split('\n'):
            if (line.startswith('+') and not line.startswith('+++')
                    and WORD.search(line) and not is_protected(line)):
                hits.append(f'{rel}: {line[:100]}')
    return hits


def run(config, root, check):
    """Applies every step in order; raises RebrandError on the first mismatch."""
    tree = Tree(root, check)
    for needed in (f'{CORE}/patches/series', 'patches/series', 'scripts/build.sh'):
        if not (tree.root / needed).exists():
            raise RebrandError(
                f'{tree.root}: not a helium-linux checkout with its submodule '
                f'(missing {needed}). Run: git submodule update --init')
    if (tree.root / MARKER).exists():
        raise RebrandError(f'{tree.root} was already rebranded. Start from a clean checkout:\n'
                           '  git checkout . && git clean -fd && '
                           'git submodule foreach "git checkout . && git clean -fd"')

    # The broad rename runs first; the targeted edits then put your own values
    # in, so nothing you configured is rewritten by a later step.
    rename_in_patches(tree, config)
    rename_in_translations(tree, config)
    rename_in_name_substitution(tree, config)
    rename_scheme(tree, config)
    rebrand_core_identity(tree, config)
    rebrand_endpoints(tree, config)
    rebrand_linux_patches(tree, config)
    rebrand_packaging(tree, config)
    install_branding(tree, config)
    write_notice(tree, config)

    remaining = leftovers(tree)
    if remaining:
        raise RebrandError('these added lines still say Helium:\n  ' + '\n  '.join(remaining))
    if not check:
        (tree.root / MARKER).write_text(
            f'{config.name} {datetime.date.today().isoformat()}\n', encoding='utf-8')
    return tree


def main():
    """CLI entrypoint"""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--tree', type=Path, required=True,
                        help='helium-linux checkout with the helium-chromium submodule')
    parser.add_argument('--check', action='store_true',
                        help='run every step and report, without writing anything')
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        tree = run(config, args.tree, args.check)
    except RebrandError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1

    print(('Would rebrand' if args.check else 'Rebranded') +
          f' {args.tree} as {config.name} ({config.slug}, {config.rdn})')
    for line in tree.log:
        print(f'  - {line}')
    if tree.warnings:
        print('Kept as-is, because these lines must match third-party code being patched:')
        for line in tree.warnings:
            print(f'  - {line}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
