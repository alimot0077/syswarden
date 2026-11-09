"""Slow-changing inventory: autostart entries, services, installed software, cleanup targets."""

import glob
import gzip
import os
import re
import shutil
import time

from . import core
from .i18n import _
from .core import HOME, run

# ---------------------------------------------------------------- service knowledge

# (pattern, Persian explanation, category). Categories: dev, optional, essential.
SERVICE_NOTES = [
    (r'^(mysql|mariadb)\.service$', 'MySQL/MariaDB database', 'dev'),
    (r'^postgresql.*\.service$', 'PostgreSQL database', 'dev'),
    (r'^(apache2|httpd)\.service$', 'Apache web server', 'dev'),
    (r'^nginx\.service$', 'Nginx web server', 'dev'),
    (r'^(docker|containerd)\.(service|socket)$', 'Docker — not needed while you are not using containers', 'dev'),
    (r'^redis(-server)?\.service$', 'Redis', 'dev'),
    (r'^mongod\.service$', 'MongoDB', 'dev'),
    (r'^memcached\.service$', 'Memcached', 'dev'),
    (r'^php.*-fpm\.service$', 'PHP-FPM', 'dev'),
    (r'^(libvirtd|virtqemud)\.service$', 'Virtual machines (libvirt)', 'dev'),
    (r'^(elasticsearch|opensearch|rabbitmq-server|jenkins|grafana-server|influxdb|minio|kafka|zookeeper|couchdb|cassandra)\.service$',
     'Heavy development/server service', 'dev'),
    (r'^snap\..*\.service$', 'Service of a snap app', 'optional'),
    (r'^ssh\.service$|^sshd\.service$', 'SSH server — not needed unless you connect to this computer remotely', 'optional'),
    (r'^(cups|cups-browsed)\.service$', 'Printing — not needed if you have no printer', 'optional'),
    (r'^ModemManager\.service$', 'Mobile broadband modems — most laptops do not need it', 'optional'),
    (r'^kdump-tools\.service$', 'Kernel crash dumps — reserves RAM; not needed on a desktop', 'optional'),
    (r'^cloud-.*\.service$', 'For cloud servers — not needed on a laptop', 'optional'),
    (r'^avahi-daemon\.service$', 'Local network discovery (printers, shares)', 'optional'),
    (r'^sysstat\.service$', 'System statistics collector', 'optional'),
    (r'^apport\.service$', 'Ubuntu crash reporter', 'optional'),
    (r'^openvpn\.service$', 'System-wide OpenVPN', 'optional'),
    (r'^bluetooth\.service$', 'Bluetooth — keep it if you use Bluetooth devices', 'optional'),
    (r'^(teamviewerd|anydesk)\.service$', 'Remote desktop access', 'optional'),
]

ESSENTIAL = re.compile(
    r'^(NetworkManager.*|systemd-.*|dbus.*|gdm.*|accounts-daemon|apparmor|polkit|udisks2|upower|'
    r'power-profiles-daemon|thermald|snapd.*|wpa_supplicant|rsyslog|cron|anacron|chrony|getty@.*|'
    r'console-setup|keyboard-setup|setvtrgb|grub.*|secureboot-db|switcheroo-control|gpu-manager|'
    r'nvidia-.*|netplan-.*|networkd-dispatcher|e2scrub_reap|dmesg|ufw|ssl-cert|rtkit-daemon|fwupd|'
    r'packagekit|colord|ubuntu-.*|unattended-upgrades|lvm2.*|blk-availability|plymouth.*|'
    r'pipewire.*|wireplumber|gnome-.*|org\.gnome\..*|xdg-.*|at-spi.*|evolution-.*|tracker-.*|'
    r'gvfs-.*|dconf|sssd|kerneloops|alsa-.*|bolt|iio-sensor-proxy|irqbalance|multipathd|'
    r'system-guard-watch|qemu-kvm|uuidd|open-vm-tools|vboxadd.*|xdg-permission-store)\.service$')

DEV_BOOT = re.compile(r'^(mysql|mariadb|postgresql.*|apache2|httpd|nginx|docker|containerd|redis(-server)?|'
                      r'mongod|memcached|php.*-fpm|libvirtd|elasticsearch|opensearch|rabbitmq-server|'
                      r'jenkins|grafana-server|influxdb|minio|kafka|zookeeper|couchdb|cassandra)\.service$')


def describe_service(unit):
    for pattern, note, category in SERVICE_NOTES:
        if re.match(pattern, unit):
            return _(note), category
    if ESSENTIAL.match(unit):
        return _('Part of the operating system'), 'essential'
    return '', 'normal'


def _show_units(units, user=False):
    if not units:
        return []
    props = 'Id,Description,ActiveState,SubState,UnitFileState,MemoryCurrent,MainPID,FragmentPath'
    scope = ['--user'] if user else []
    ok, out, _err = run(['systemctl', *scope, 'show', '-p', props, '--', *units], timeout=30)
    if not ok and not out:
        return []
    rows = []
    for block in out.strip().split('\n\n'):
        row = {}
        for line in block.splitlines():
            key, _sep, value = line.partition('=')
            row[key] = value
        if not row.get('Id'):
            continue
        mem = row.get('MemoryCurrent', '')
        mem = int(mem) if mem.isdigit() and int(mem) < 1 << 50 else None
        note, category = describe_service(row['Id'])
        rows.append({
            'unit': row['Id'], 'description': row.get('Description', ''),
            'active': row.get('ActiveState') == 'active', 'sub': row.get('SubState', ''),
            'enabled': row.get('UnitFileState') in ('enabled', 'enabled-runtime', 'linked'),
            'file_state': row.get('UnitFileState', ''), 'memory': mem,
            'pid': int(row.get('MainPID') or 0), 'scope': 'user' if user else 'system',
            'note': note, 'category': category,
        })
    return rows


def list_services(user=False):
    """Enabled or currently running services, with memory and an explanation."""
    scope = ['--user'] if user else []
    names = set()
    ok, out, _err = run(['systemctl', *scope, 'list-unit-files', '--type=service', '--state=enabled',
                      '--no-legend', '--plain'], timeout=20)
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].endswith('.service') and not parts[0].endswith('@.service'):
            names.add(parts[0])
    ok, out, _err = run(['systemctl', *scope, 'list-units', '--type=service', '--state=running',
                      '--no-legend', '--plain'], timeout=20)
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].endswith('.service'):
            names.add(parts[0])
    rows = _show_units(sorted(names), user)
    order = {'dev': 0, 'optional': 1, 'normal': 2, 'essential': 3}
    rows.sort(key=lambda r: (order[r['category']], -(r['memory'] or 0)))
    return rows


def boot_dev_services():
    ok, out, _err = run(['systemctl', 'list-unit-files', '--type=service', '--state=enabled',
                      '--no-legend', '--plain'], timeout=20)
    return [line.split()[0].removesuffix('.service') for line in out.splitlines()
            if line.split() and DEV_BOOT.match(line.split()[0])]


# ---------------------------------------------------------------- autostart

USER_AUTOSTART = os.path.join(HOME, '.config', 'autostart')
SYSTEM_AUTOSTART = ['/etc/xdg/autostart']
PROTECTED_AUTOSTART = re.compile(
    r'^(org\.gnome\..*|gnome-.*|at-spi.*|ibus.*|im-launch|xdg-user-dirs.*|user-dirs-update.*|'
    r'pipewire.*|spice-vdagent|snap-userd-autostart|ubuntu-.*|geoclue-demo-agent|'
    r'org\.freedesktop\..*|tracker-.*|polkit.*|nm-applet|print-applet|update-notifier|'
    r'gsd-.*|xdg-desktop-portal.*|mutter.*|orca-autostart|ding.*)\.desktop$')


def _keyfile(path):
    from gi.repository import GLib
    kf = GLib.KeyFile()
    try:
        kf.load_from_file(path, GLib.KeyFileFlags.KEEP_TRANSLATIONS)
        return kf
    except GLib.Error:
        return None


def _get(kf, key, default=None, boolean=False, locale=True):
    from gi.repository import GLib
    try:
        if boolean:
            return kf.get_boolean('Desktop Entry', key)
        if locale:
            return kf.get_locale_string('Desktop Entry', key, None)
        return kf.get_string('Desktop Entry', key)
    except GLib.Error:
        return default


def _shown_in_gnome(kf):
    only = _get(kf, 'OnlyShowIn', '', locale=False) or ''
    never = _get(kf, 'NotShowIn', '', locale=False) or ''
    desk = {'GNOME', 'Unity', 'ubuntu'}
    if only and not desk & set(filter(None, only.split(';'))):
        return False
    return not (desk & set(filter(None, never.split(';'))))


def list_autostart():
    entries = {}
    for directory in SYSTEM_AUTOSTART + [USER_AUTOSTART]:
        for path in sorted(glob.glob(os.path.join(directory, '*.desktop'))):
            kf = _keyfile(path)
            if not kf:
                continue
            name = os.path.basename(path)
            user = directory == USER_AUTOSTART
            base = entries.get(name, {})
            hidden = _get(kf, 'Hidden', False, boolean=True)
            enabled = _get(kf, 'X-GNOME-Autostart-enabled', True, boolean=True) and not hidden
            entry = {
                'id': name, 'path': path, 'user': user,
                'system_path': base.get('system_path') if user else path,
                'name': _get(kf, 'Name') or base.get('name') or name,
                'comment': _get(kf, 'Comment') or base.get('comment') or '',
                'icon': _get(kf, 'Icon', locale=False) or base.get('icon'),
                'exec': _get(kf, 'Exec', locale=False) or base.get('exec') or '',
                'enabled': enabled,
                'visible': _shown_in_gnome(kf) if not user else base.get('visible', True),
                'phase': _get(kf, 'X-GNOME-Autostart-Phase', locale=False) or base.get('phase'),
            }
            entry['protected'] = bool(entry['phase']) or bool(PROTECTED_AUTOSTART.match(name))
            entries[name] = entry
    rows = [e for e in entries.values() if e['visible']]
    rows.sort(key=lambda e: (e['protected'], not e['user'], e['name'].lower()))
    return rows


def set_autostart(entry, enabled):
    """Per-user override, the same way GNOME Tweaks does it; never touches /etc."""
    from gi.repository import GLib
    os.makedirs(USER_AUTOSTART, exist_ok=True)
    target = os.path.join(USER_AUTOSTART, entry['id'])
    if not os.path.exists(target):
        source = entry.get('system_path') or entry['path']
        shutil.copyfile(source, target)
    kf = _keyfile(target)
    if kf is None:
        return False, _('The file cannot be read.')
    kf.set_boolean('Desktop Entry', 'X-GNOME-Autostart-enabled', enabled)
    if enabled:
        kf.set_boolean('Desktop Entry', 'Hidden', False)
    try:
        kf.save_to_file(target)
    except GLib.Error as error:
        return False, str(error)
    return True, ''


# ---------------------------------------------------------------- apt packages

PROTECTED_PKGS = re.compile(
    r'^(ubuntu-.*|gnome-shell.*|gdm3|gnome-session.*|mutter.*|systemd.*|apt|dpkg|sudo|libc6|'
    r'linux-(image|generic|headers|modules).*|network-manager.*|python3(\.\d+)?|python3-minimal|'
    r'nautilus|gnome-control-center|xorg|xwayland|grub.*|shim.*|initramfs-tools.*|snapd|'
    r'policykit-1|polkitd|dbus.*|bash|coreutils.*|login|passwd|plymouth.*|pipewire.*|wireplumber|'
    r'gnome-terminal|ptyxis|libgtk.*|gir1\.2-.*|libglib.*|gjs|mesa.*|libgl.*|firmware.*|'
    r'linux-firmware|wpasupplicant|openssh-client|ca-certificates|tzdata|locales|efibootmgr|mokutil)$')
BIN_DIRS = ('/usr/bin/', '/usr/sbin/', '/bin/', '/sbin/', '/usr/games/', '/usr/local/bin/', '/opt/')


def _atime_reliable():
    try:
        with open('/proc/mounts') as handle:
            for line in handle:
                parts = line.split()
                if parts[1] == '/':
                    return 'noatime' not in parts[3]
    except OSError:
        pass
    return False


def _meta_baseline():
    """Packages pulled in directly by Ubuntu's own metapackages (part of the OS install)."""
    ok, out, _err = run(['dpkg-query', '-W', '-f=${Package}\t${Status}\n', 'ubuntu-*'], timeout=20)
    metas = [l.split('\t')[0] for l in out.splitlines() if l.endswith('install ok installed')]
    if not metas:
        return set()
    ok, out, _err = run(['dpkg-query', '-W', '-f=${Depends}, ${Recommends}, ${Pre-Depends}\n', *metas], timeout=20)
    names = set(metas)
    for token in re.split(r'[,|]', out):
        token = token.strip().split(' ')[0].split(':')[0]
        if token:
            names.add(token)
    return names


# Commands the Ubuntu installer / system tools run themselves (not the user's choice).
_INSTALLER_CMD = re.compile(r'Debug::pkgDepCache|--option=Dpkg::Options|curtin|subiquity|unattended-upgrade')


def _apt_history():
    """pkg -> (date, commandline) for packages explicitly installed with apt."""
    history = {}
    files = sorted(glob.glob('/var/log/apt/history.log*'), key=lambda p: os.path.getmtime(p))
    for path in files:
        try:
            opener = gzip.open if path.endswith('.gz') else open
            with opener(path, 'rt', errors='replace') as handle:
                text = handle.read()
        except OSError:
            continue
        for block in text.split('\n\n'):
            fields = {}
            for line in block.splitlines():
                key, _sep, value = line.partition(': ')
                fields[key] = value
            if 'Install' not in fields:
                continue
            cmd = fields.get('Commandline', '')
            date = fields.get('Start-Date', '')
            if _INSTALLER_CMD.search(cmd):
                continue
            for item in re.finditer(r'([a-z0-9][a-z0-9+.\-]*)(?::[a-z0-9]+)? \(([^)]*)\)', fields['Install']):
                if 'automatic' not in item.group(2):
                    history[item.group(1)] = (date, cmd)
    return history


def _dpkg_installs():
    installs = {}
    for path in sorted(glob.glob('/var/log/dpkg.log*'), key=lambda p: os.path.getmtime(p)):
        try:
            opener = gzip.open if path.endswith('.gz') else open
            with opener(path, 'rt', errors='replace') as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) > 3 and parts[2] == 'install':
                        installs[parts[3].split(':')[0]] = time.mktime(
                            time.strptime(parts[0] + ' ' + parts[1], '%Y-%m-%d %H:%M:%S'))
        except (OSError, ValueError):
            continue
    return installs


def _installer_day(manual):
    """Manual packages untouched since the day the OS was installed."""
    stamps = {}
    for pkg in manual:
        path = _list_file(pkg)
        if path:
            stamps[pkg] = os.path.getmtime(path)
    if not stamps:
        return set()
    first = min(stamps.values())
    return {p for p, t in stamps.items() if t - first < 86400}


def _list_file(pkg):
    for candidate in (f'/var/lib/dpkg/info/{pkg}.list', *glob.glob(f'/var/lib/dpkg/info/{pkg}:*.list')):
        if os.path.exists(candidate):
            return candidate
    return None


def _usage(pkg, reliable):
    """(last_used_timestamp, never_used, has_binaries, has_desktop_file)."""
    path = _list_file(pkg)
    if not path:
        return None, False, False, False
    last = None
    never = True
    has_bin = has_desktop = False
    try:
        with open(path) as handle:
            files = [line.strip() for line in handle]
    except OSError:
        return None, False, False, False
    extra = []
    for f in files:
        if f.endswith('.desktop') and f.startswith('/usr/share/applications/'):
            has_desktop = True
            target = _desktop_exec(f)
            if target:
                extra.append(target)
    for f in files + extra:
        if not f.startswith(BIN_DIRS) and f not in extra:
            continue
        try:
            st = os.lstat(f)
        except OSError:
            continue
        if not (st.st_mode & 0o111) or os.path.isdir(f) or os.path.islink(f):
            continue
        has_bin = True
        if reliable:
            last = max(last or 0, st.st_atime)
            if st.st_atime - st.st_ctime > 300:
                never = False
    if not has_bin:
        never = False
    return last, never, has_bin, has_desktop


def _desktop_exec(path):
    """Real binary behind a .desktop launcher (apps living in /usr/lib or /opt)."""
    kf = _keyfile(path)
    command = _get(kf, 'Exec', locale=False) if kf else None
    if not command:
        return None
    first = command.split()[0].strip('"')
    found = first if first.startswith('/') else shutil.which(first)
    return os.path.realpath(found) if found else None


def list_apt_packages():
    """Packages the user (or an agent) asked for, beyond what Ubuntu installed by itself."""
    ok, out, _err = run(['apt-mark', 'showmanual'], timeout=30)
    manual = set(out.split())
    baseline = _meta_baseline()
    history = _apt_history()
    candidates = sorted(set(history) & manual | (manual - baseline - _installer_day(manual)))
    if not candidates:
        return []
    ok, out, _err = run(['dpkg-query', '-W', '-f=${Package}\t${Installed-Size}\t${Section}\t${Priority}\t'
                      '${Essential}\t${db:Status-Abbrev}\t${binary:Summary}\n', *candidates], timeout=30)
    installs = _dpkg_installs()
    reliable = _atime_reliable()
    autoremove = set(autoremovable())
    rows = []
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) < 7 or not parts[5].startswith('ii'):
            continue
        name, size, section, priority, essential, _status, summary = parts[:7]
        if essential == 'yes' or priority in ('required', 'important'):
            continue
        last, never, has_bin, has_desktop = _usage(name, reliable)
        installed = installs.get(name)
        if installed is None:
            lst = _list_file(name)
            installed = os.path.getctime(lst) if lst else None
        hist = history.get(name)
        age = time.time() - installed if installed else None
        protected = bool(PROTECTED_PKGS.match(name))
        unused = not protected and has_bin and reliable and age and age > 14 * 86400 and (
            never or (last and time.time() - last > 90 * 86400))
        rows.append({
            'name': name, 'size': int(size or 0) * 1024, 'section': section, 'summary': summary,
            'installed': installed, 'command': hist[1] if hist else None,
            'last_used': last if has_bin and not never else None, 'never_used': never and has_bin,
            'has_bin': has_bin, 'gui': has_desktop, 'unused': bool(unused),
            'dev': section in ('devel', 'libdevel', 'python', 'javascript', 'php', 'ruby', 'perl', 'java')
                   or name.endswith('-dev'),
            'protected': protected, 'autoremove': name in autoremove,
        })
    rows.sort(key=lambda r: (not r['unused'], -r['size']))
    return rows


def simulate_removal(packages, purge=False):
    """(ok, removed_names, blocked_names, freed_bytes_estimate, error)."""
    argv = ['apt-get', '-s', '--purge' if purge else '--no-purge', 'remove', *packages]
    ok, out, err = run(argv, timeout=60)
    if not ok:
        return False, [], [], 0, (err or out).strip()[-400:]
    removed = re.findall(r'^(?:Remv|Purg) (\S+)', out, re.M)
    removed = [r.split(':')[0] for r in removed]
    blocked = [r for r in removed if PROTECTED_PKGS.match(r)]
    freed = 0
    if removed:
        ok, sizes, _err = run(['dpkg-query', '-W', '-f=${Installed-Size}\n', *removed], timeout=20)
        freed = sum(int(s) for s in sizes.split() if s.isdigit()) * 1024
    return True, removed, blocked, freed, ''


def remove_packages(packages, purge=False):
    for p in packages:
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.\-]*(:[a-z0-9]+)?', p):
            return False, _('Invalid package name: {name}').format(name=p)
    argv = ['pkexec', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-y',
            'purge' if purge else 'remove', *packages]
    ok, out, err = run(argv, timeout=900)
    return ok, (err or out).strip()[-500:]


def autoremovable():
    ok, out, _err = run(['apt-get', '-s', 'autoremove'], timeout=60)
    return [r.split(':')[0] for r in re.findall(r'^Remv (\S+)', out, re.M)]


# ---------------------------------------------------------------- snaps & flatpaks

BASE_SNAPS = re.compile(r'^(core\d*|snapd|bare|gnome-\d+-.*|gtk-common-themes|mesa-.*|kf\d+-.*|'
                        r'snapd-desktop-integration|desktop-security-center|prompting-client|'
                        r'firmware-updater|snap-store|canonical-livepatch)$')


def list_snaps():
    ok, out, _err = run(['snap', 'list', '--all', '--color=never', '--unicode=never'], timeout=30)
    rows = {}
    old = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        name, version, rev = parts[0], parts[1], parts[2]
        notes = parts[-1] if len(parts) >= 6 else ''
        path = f'/var/lib/snapd/snaps/{name}_{rev}.snap'
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if 'disabled' in notes:
            old.append({'name': name, 'rev': rev, 'size': size})
            continue
        home = os.path.join(HOME, 'snap', name)
        last = None
        for candidate in (os.path.join(home, 'current'), home):
            if os.path.exists(candidate):
                last = os.path.getmtime(candidate)
                break
        rows[name] = {'name': name, 'version': version, 'rev': rev, 'size': size, 'last_used': last,
                      'base': bool(BASE_SNAPS.match(name)), 'kind': 'snap'}
    result = sorted(rows.values(), key=lambda r: (r['base'], -r['size']))
    return result, old


def list_flatpaks():
    ok, out, _err = run(['flatpak', 'list', '--app', '--columns=application,name,size,installation'], timeout=30)
    rows = []
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) < 4:
            continue
        app, name, size, where = parts[:4]
        home = os.path.join(HOME, '.var', 'app', app)
        rows.append({'name': name, 'id': app, 'size_text': size, 'installation': where,
                     'last_used': os.path.getmtime(home) if os.path.exists(home) else None,
                     'kind': 'flatpak'})
    return rows


def remove_snap(name):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name):
        return False, _('Invalid name')
    ok, out, err = run(['pkexec', 'snap', 'remove', name], timeout=600)
    return ok, (err or out).strip()[-300:]


def remove_flatpak(app_id, installation):
    if not re.fullmatch(r'[A-Za-z0-9._-]+', app_id):
        return False, _('Invalid id')
    ok, out, err = run(['flatpak', 'uninstall', '-y', '--user' if installation == 'user' else '--system',
                        app_id], timeout=600)
    return ok, (err or out).strip()[-300:]


# ---------------------------------------------------------------- cleanup

def dir_size(path, limit_seconds=8):
    total = 0
    deadline = time.time() + limit_seconds
    stack = [path]
    while stack:
        if time.time() > deadline:
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        continue
        except OSError:
            continue
    return total


USER_CACHES = [
    ('pip', 'pip cache', os.path.join(HOME, '.cache', 'pip')),
    ('npm', 'npm cache', os.path.join(HOME, '.npm', '_cacache')),
    ('yarn', 'Yarn cache', os.path.join(HOME, '.cache', 'yarn')),
    ('pnpm', 'pnpm store', os.path.join(HOME, '.local', 'share', 'pnpm', 'store')),
    ('composer', 'Composer cache', os.path.join(HOME, '.cache', 'composer')),
    ('gobuild', 'Go build cache', os.path.join(HOME, '.cache', 'go-build')),
    ('gradle', 'Gradle cache', os.path.join(HOME, '.gradle', 'caches')),
    ('playwright', 'Playwright browsers', os.path.join(HOME, '.cache', 'ms-playwright')),
    ('puppeteer', 'Puppeteer browsers', os.path.join(HOME, '.cache', 'puppeteer')),
    ('thumbs', 'Thumbnails', os.path.join(HOME, '.cache', 'thumbnails')),
    ('trash', 'Trash', os.path.join(HOME, '.local', 'share', 'Trash')),
]


def cleanup_targets(old_snaps=None):
    rows = []
    auto = autoremovable()
    if auto:
        ok, sizes, _err = run(['dpkg-query', '-W', '-f=${Installed-Size}\n', *auto], timeout=20)
        size = sum(int(s) for s in sizes.split() if s.isdigit()) * 1024
        rows.append({'id': 'autoremove', 'title': _('Orphaned packages (autoremove)'),
                     'detail': _('{n} packages no other software needs anymore: {names}').format(
                         n=core.fa(len(auto)), names=_(', ').join(auto[:8]) + ('…' if len(auto) > 8 else '')),
                     'size': size, 'root': True})
    apt_cache = sum(os.path.getsize(p) for p in glob.glob('/var/cache/apt/archives/*.deb'))
    if apt_cache > 1024 ** 2:
        rows.append({'id': 'aptclean', 'title': _('Downloaded apt packages'),
                     'detail': _('Package files kept after installation; they are not needed.'),
                     'size': apt_cache, 'root': True})
    if old_snaps:
        rows.append({'id': 'oldsnaps', 'title': _('Old snap revisions'),
                     'detail': _('{n} disabled revisions snap keeps for rollback.').format(n=core.fa(len(old_snaps))),
                     'size': sum(s['size'] for s in old_snaps), 'root': True, 'snaps': old_snaps})
    ok, out, _err = run(['journalctl', '--disk-usage'], timeout=10)
    match = re.search(r'take up ([\d.]+)([KMGT])', out)
    if match:
        size = float(match.group(1)) * 1024 ** ' KMGT'.index(match.group(2))
        if size > 300 * 1024 ** 2:
            rows.append({'id': 'journal', 'title': _('System logs (journal)'),
                         'detail': _('Old logs; the newest 200 MB are kept.'),
                         'size': size, 'root': True})
    for cid, title, path in USER_CACHES:
        if os.path.isdir(path):
            size = dir_size(path)
            if size > 20 * 1024 ** 2:
                rows.append({'id': 'cache:' + cid, 'title': _(title), 'detail': path.replace(HOME, '~'),
                             'size': size, 'root': False, 'path': path})
    return rows


def clean(target):
    tid = target['id']
    if tid == 'autoremove':
        ok, out, err = run(['pkexec', 'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-y', 'autoremove'],
                           timeout=900)
    elif tid == 'aptclean':
        ok, out, err = run(['pkexec', 'apt-get', 'clean'], timeout=120)
    elif tid == 'journal':
        ok, out, err = run(['pkexec', 'journalctl', '--vacuum-size=200M'], timeout=120)
    elif tid == 'oldsnaps':
        commands = []
        for snap in target.get('snaps', []):
            if re.fullmatch(r'[a-z0-9-]+', snap['name']) and snap['rev'].isdigit():
                commands.append(f'snap remove {snap["name"]} --revision={snap["rev"]}')
        if not commands:
            return True, ''
        ok, out, err = run(['pkexec', '/bin/sh', '-c', '; '.join(commands)], timeout=900)
    elif tid == 'cache:trash':
        ok, out, err = run(['gio', 'trash', '--empty'], timeout=120)
    elif tid.startswith('cache:'):
        path = target['path']
        if not path.startswith(HOME + os.sep):
            return False, _('Invalid path')
        for entry in os.listdir(path):
            full = os.path.join(path, entry)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
            except OSError:
                pass
        return True, ''
    else:
        return False, _('Unknown')
    return ok, (err or out).strip()[-300:]
