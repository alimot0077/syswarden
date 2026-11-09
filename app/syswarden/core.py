"""Live system sampling, process metadata and safe process/power actions."""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections import deque

import psutil

from .i18n import _, is_rtl, num

HOME = os.path.expanduser('~')
RUNTIME_DIR = os.path.join(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'),
                           'syswarden')
STATE_FILE = os.path.join(RUNTIME_DIR, 'state.json')
CONFIG_DIR = os.path.join(HOME, '.config', 'syswarden')
IGNORED_FILE = os.path.join(CONFIG_DIR, 'ignored.json')
NCPU = psutil.cpu_count() or 1
TOTAL_MEM = psutil.virtual_memory().total
MY_UID = os.getuid()
MY_PID = os.getpid()

# ---------------------------------------------------------------- formatting

def fa(value):
    """Localized digits (kept under this name; Persian digits only in Persian mode)."""
    return num(value)


def human_bytes(n):
    if n is None:
        return '—'
    n = float(n)
    if n <= 0:
        return '—'
    units = (_('B'), _('KB'), _('MB'), _('GB'), _('TB'))
    for i, unit in enumerate(units):
        if abs(n) < 1024 or i == len(units) - 1:
            return num(f'{n:.0f}' if i < 2 else f'{n:.1f}') + ' ' + unit
        n /= 1024
    return num(n)


def human_duration(seconds):
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        text = _('{n} d').format(n=num(days))
        return text + (' ' + _('{n} h').format(n=num(hours)) if hours else '')
    if hours:
        text = _('{n} h').format(n=num(hours))
        return text + (' ' + _('{n} min').format(n=num(minutes)) if minutes else '')
    if minutes:
        return _('{n} min').format(n=num(minutes))
    return _('{n} s').format(n=num(seconds))


def human_ago(timestamp):
    if not timestamp:
        return '—'
    delta = time.time() - timestamp
    if delta < 120:
        return _('just now')
    return _('{t} ago').format(t=human_duration(delta))


def pct(value):
    return num(f'{value:.0f}%')


def sep():
    """Separator; in RTL mode wrapped with RLMs so neighbouring numbers do not merge."""
    return ' \u200f•\u200f ' if is_rtl() else ' · '


# ---------------------------------------------------------------- small utils

def run(argv, timeout=20):
    """Run a command; return (ok, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as error:
        return False, '', str(error)


def readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def load_ignored():
    try:
        with open(IGNORED_FILE) as handle:
            return set(json.load(handle))
    except (OSError, ValueError):
        return set()


def save_ignored(ids):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = IGNORED_FILE + '.tmp'
    with open(tmp, 'w') as handle:
        json.dump(sorted(ids), handle, ensure_ascii=False, indent=1)
    os.replace(tmp, IGNORED_FILE)


def add_ignored(issue_id):
    ids = load_ignored()
    ids.add(issue_id)
    save_ignored(ids)


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w') as handle:
        json.dump(data, handle, ensure_ascii=False)
    os.replace(tmp, path)


def read_state(max_age=30):
    """The watcher's latest state, or None when it is missing or stale."""
    try:
        with open(STATE_FILE) as handle:
            state = json.load(handle)
        if time.time() - state.get('ts', 0) <= max_age:
            return state
    except (OSError, ValueError):
        pass
    return None


# ---------------------------------------------------------------- sensors

def read_temps():
    out = {'cpu': None, 'cpu_crit': 100.0, 'nvme': None, 'wifi': None, 'acpi': None, 'cores': []}
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return out
    for name, entries in sensors.items():
        if not entries:
            continue
        if name in ('coretemp', 'k10temp', 'zenpower', 'cpu_thermal'):
            for entry in entries:
                label = entry.label or ''
                if label.startswith('Package') or label in ('Tctl', 'Tdie'):
                    out['cpu'] = entry.current
                    if entry.critical:
                        out['cpu_crit'] = entry.critical
                elif label.startswith('Core'):
                    out['cores'].append(entry.current)
            if out['cpu'] is None:
                out['cpu'] = max(e.current for e in entries)
        elif name == 'nvme' and out['nvme'] is None:
            composite = [e for e in entries if (e.label or '').lower() == 'composite']
            out['nvme'] = (composite or entries)[0].current
        elif name.startswith('mt79') or name.startswith('iwlwifi'):
            out['wifi'] = entries[0].current
        elif name == 'acpitz':
            out['acpi'] = entries[0].current
    if out['cpu'] is None:
        out['cpu'] = out['acpi']
    return out


def read_fans():
    try:
        return [f.current for entries in psutil.sensors_fans().values() for f in entries]
    except (AttributeError, OSError):
        return []


def power_profile():
    ok, out, _err = run(['powerprofilesctl', 'get'], timeout=5)
    return out.strip() if ok else None


def set_power_profile(profile):
    ok, _out, err = run(['powerprofilesctl', 'set', profile], timeout=10)
    return ok, _('Power mode set to {p}.').format(p=profile) if ok else err.strip()


def system_metrics():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    disk = psutil.disk_usage('/')
    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)
    battery = None
    try:
        b = psutil.sensors_battery()
        if b:
            battery = {'percent': b.percent, 'plugged': b.power_plugged}
    except (AttributeError, OSError):
        pass
    temps = read_temps()
    return {
        'cpu': psutil.cpu_percent(None),
        'per_cpu': psutil.cpu_percent(None, percpu=True),
        'mem_total': vm.total, 'mem_used': vm.total - vm.available,
        'mem_available': vm.available, 'mem_pct': 100 - vm.available * 100 / vm.total,
        'swap_total': sw.total, 'swap_used': sw.used, 'swap_pct': sw.percent,
        'disk_total': disk.total, 'disk_used': disk.used, 'disk_free': disk.free,
        'disk_pct': disk.percent,
        'load': load, 'temps': temps, 'temp': temps['cpu'], 'fans': read_fans(),
        'battery': battery, 'uptime': time.time() - psutil.boot_time(),
    }


# ---------------------------------------------------------------- app grouping

_UNESCAPE = re.compile(r'\\x([0-9a-fA-F]{2})')
_APP_SCOPE = re.compile(r'^app-(?:gnome-|flatpak-|dbus-:[\d.]+-)?(.+?)(?:-\d+|-[0-9a-f]{32})\.scope$')
_APP_SERVICE = re.compile(r'^app-(?:gnome-|flatpak-)?(.+?)@[^@]*\.service$')
_SNAP_SCOPE = re.compile(r'^snap\.([^.]+)\.')
_app_name_cache = {}


def _unescape(text):
    return _UNESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def cgroup_info(pid):
    """(app_id, unit, unit_scope) derived from the process's systemd cgroup."""
    try:
        with open(f'/proc/{pid}/cgroup') as handle:
            path = handle.read().strip().splitlines()[-1].split('::', 1)[-1]
    except (OSError, IndexError):
        return None, None, None
    leaf = path.rsplit('/', 1)[-1]
    in_user = '/user@' in path
    if leaf.endswith('.service'):
        match = _APP_SERVICE.match(leaf)
        if in_user and match:
            return _unescape(match.group(1)), None, None
        return None, _unescape(leaf), 'user' if in_user else 'system'
    if leaf.endswith('.scope'):
        match = _APP_SCOPE.match(leaf)
        if match:
            return _unescape(match.group(1)), None, None
        match = _SNAP_SCOPE.match(leaf)
        if match:
            return 'snap:' + match.group(1), None, None
        if leaf.startswith('docker-'):
            return 'docker', None, None
    return None, None, None


def app_display(app_id):
    """Human name and icon name for a desktop app id (cached; works without a display)."""
    if app_id in _app_name_cache:
        return _app_name_cache[app_id]
    result = (app_id, None)
    if app_id.startswith('snap:'):
        result = (app_id[5:], app_id[5:])
    else:
        try:
            import gi
            gi.require_version('Gio', '2.0')
            from gi.repository import Gio
            info = None
            for candidate in (app_id, app_id.lower(), app_id.split('.')[-1]):
                try:
                    info = Gio.DesktopAppInfo.new(candidate + '.desktop')
                except TypeError:
                    info = None
                if info:
                    break
            if info:
                icon = info.get_icon()
                icon_name = icon.to_string() if icon else None
                result = (info.get_name() or app_id, icon_name)
        except (ImportError, ValueError):
            pass
    _app_name_cache[app_id] = result
    return result


# Developer daemons get their own row even when an IDE/agent app launched them.
_BREAKOUT = re.compile(r'^(mysqld|mariadbd|postgres|redis-server|mongod|memcached|httpd|apache2|nginx|'
                       r'php[\d.]*|php-fpm.*|node|nodejs|deno|bun|python[\d.]*|ruby[\d.]*|java|uvicorn|'
                       r'gunicorn|esbuild|qemu-system-.*|docker.*|containerd.*)$')

_INTERP = re.compile(r'^(python[\d.]*|node|nodejs|java|php[\d.]*|ruby[\d.]*|perl|deno|bun|bash|sh|dotnet)$')


def pretty_name(name, cmdline):
    """'node · vite' instead of a bare interpreter name."""
    if _INTERP.match(name or '') and len(cmdline) > 1:
        skip_next = False
        for arg in cmdline[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg == '-c':
                return f'{name} -c'
            if arg in ('-m', '-jar', '-cp', '-classpath'):
                if arg == '-m':
                    continue
                skip_next = arg in ('-cp', '-classpath')
                continue
            if arg.startswith('-'):
                continue
            base = os.path.basename(arg.rstrip('/'))
            if base:
                return f'{name} · {base}'[:70]
    return name


# ---------------------------------------------------------------- sampling

class Sampler:
    """Samples every process; keeps CPU history per process and for the system."""

    def __init__(self, history=180):
        self.history = history
        self.prev = {}
        self.static = {}
        self.proc_hist = {}
        self.sys_hist = {k: deque(maxlen=history) for k in ('cpu', 'mem', 'temp')}
        self.sample_times = deque(maxlen=history)
        self.user_manager_pids = set()
        psutil.cpu_percent(None)
        psutil.cpu_percent(None, percpu=True)

    def _static(self, proc):
        info = {'name': proc.name(), 'cmdline': [], 'exe': None, 'user': None, 'uid': None}
        try:
            info['cmdline'] = proc.cmdline()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            pass
        try:
            info['exe'] = proc.exe() or None
        except (psutil.AccessDenied, psutil.ZombieProcess):
            pass
        try:
            info['user'] = proc.username()
            info['uid'] = proc.uids().real
        except (psutil.AccessDenied, KeyError):
            pass
        app_id, unit, scope = cgroup_info(proc.pid)
        info['app_id'], info['unit'], info['unit_scope'] = app_id, unit, scope
        info['display'] = pretty_name(info['name'], info['cmdline'])
        if app_id and not _BREAKOUT.match(info['name']):
            group_label, icon = app_display(app_id)
            info['group'] = 'app:' + group_label      # merge scopes of the same app (e.g. two Chrome launches)
        elif unit:
            group_label, icon = unit.removesuffix('.service'), None
            info['group'] = 'unit:' + unit
        else:
            group_label, icon = info['display'], None
            info['group'] = 'name:' + info['display']
        info['group_label'], info['icon'] = group_label, icon
        info['kernel'] = not info['cmdline'] and info['exe'] is None and proc.pid != 1
        if info['name'] == 'systemd' and '--user' in info['cmdline']:
            self.user_manager_pids.add(proc.pid)
        return info

    def sample(self):
        now = time.monotonic()
        wall = time.time()
        procs = []
        seen = set()
        for proc in psutil.process_iter():
            try:
                with proc.oneshot():
                    ctime = proc.create_time()
                    key = (proc.pid, ctime)
                    static = self.static.get(key)
                    if static is None:
                        static = self._static(proc)
                        self.static[key] = static
                    times = proc.cpu_times()
                    total = times.user + times.system
                    rss = proc.memory_info().rss
                    status = proc.status()
                    ppid = proc.ppid()
                    threads = proc.num_threads()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
            seen.add(key)
            prev = self.prev.get(key)
            cpu = (total - prev[0]) * 100 / max(1e-3, now - prev[1]) if prev else 0.0
            self.prev[key] = (total, now)
            hist = self.proc_hist.get(key)
            if hist is None:
                hist = self.proc_hist[key] = deque(maxlen=self.history)
            if prev:
                hist.append(cpu)
            elapsed = max(1.0, wall - ctime)
            procs.append({
                'pid': proc.pid, 'ppid': ppid, 'ctime': ctime, 'key': f'{proc.pid}:{ctime:.2f}',
                'cpu': max(0.0, cpu), 'cpu_time': total, 'avg_cpu': total * 100 / elapsed,
                'elapsed': elapsed, 'rss': rss, 'mem_pct': rss * 100 / TOTAL_MEM,
                'status': status, 'threads': threads, **static,
            })
        for key in [k for k in self.prev if k not in seen]:
            self.prev.pop(key, None)
            self.static.pop(key, None)
            self.proc_hist.pop(key, None)
        metrics = system_metrics()
        self.sys_hist['cpu'].append(metrics['cpu'])
        self.sys_hist['mem'].append(metrics['mem_pct'])
        self.sys_hist['temp'].append(metrics['temp'] or 0)
        self.sample_times.append(now)
        return {'ts': wall, 'procs': procs, 'metrics': metrics}

    def recent_cpu(self, key_tuple, seconds):
        """Average CPU over the last `seconds`, or None when history is shorter."""
        hist = self.proc_hist.get(key_tuple)
        if not hist or len(self.sample_times) < 2:
            return None
        span = self.sample_times[-1] - self.sample_times[0]
        interval = span / max(1, len(self.sample_times) - 1)
        needed = max(2, int(seconds / max(0.5, interval)))
        if len(hist) < needed:
            return None
        values = list(hist)[-needed:]
        return sum(values) / len(values)


# ---------------------------------------------------------------- deeper checks

_TMP_ROOTS = ('/tmp/', '/var/tmp/', '/dev/shm/')
_PATH_ARG = re.compile(r'(?:^|=)(/[^\s=]+)')
_DELETED_IGNORE = ('/memfd:', 'memfd:', '/dev/shm/', '/SYSV', '/drm mm object', '/[aio]',
                   '/run/user/', '/tmp/.X', '/tmp/.ICE', 'anon_inode', '/dev/zero')


def missing_paths(cmdline):
    """Paths named on the command line under temp dirs that no longer exist."""
    missing = []
    for arg in cmdline[1:]:
        for match in _PATH_ARG.finditer(arg):
            path = match.group(1)
            # A file that is merely not created yet still has its folder; a
            # cleaned-up session has lost the folder too.
            if (path.startswith(_TMP_ROOTS) and not os.path.exists(path)
                    and not os.path.exists(os.path.dirname(path.rstrip('/')))):
                missing.append(path)
    return missing


def deleted_state(pid):
    """(cwd_deleted, exe_deleted, deleted_file_count) for a process we can inspect."""
    cwd = readlink(f'/proc/{pid}/cwd') or ''
    exe = readlink(f'/proc/{pid}/exe') or ''
    count = 0
    try:
        for fd in os.listdir(f'/proc/{pid}/fd'):
            target = readlink(f'/proc/{pid}/fd/{fd}')
            if target and target.endswith(' (deleted)') and not target.startswith(_DELETED_IGNORE):
                count += 1
    except OSError:
        pass
    return cwd.endswith(' (deleted)'), exe.endswith(' (deleted)'), count


def process_details(pid):
    """Everything the detail dialog shows; tolerant of permission errors."""
    details = {'pid': pid}
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            details['name'] = proc.name()
            details['status'] = proc.status()
            details['ctime'] = proc.create_time()
            details['ppid'] = proc.ppid()
            details['nice'] = proc.nice()
            details['threads'] = proc.num_threads()
            details['rss'] = proc.memory_info().rss
        for attr in ('cmdline', 'exe', 'cwd', 'username'):
            try:
                details[attr] = getattr(proc, attr)()
            except (psutil.AccessDenied, psutil.ZombieProcess):
                details[attr] = None
        chain = []
        parent = proc.parent()
        while parent and len(chain) < 8:
            chain.append(f'{parent.name()} ({parent.pid})')
            parent = parent.parent()
        details['parents'] = chain
        try:
            details['children'] = len(proc.children(recursive=True))
        except psutil.Error:
            details['children'] = 0
        try:
            details['ports'] = sorted({c.laddr.port for c in proc.net_connections('inet')
                                       if c.status == psutil.CONN_LISTEN})
        except (psutil.AccessDenied, AttributeError):
            details['ports'] = []
    except psutil.NoSuchProcess:
        return None
    details['app_id'], details['unit'], details['unit_scope'] = cgroup_info(pid)
    details['cwd_deleted'], details['exe_deleted'], details['deleted_files'] = deleted_state(pid)
    details['missing'] = missing_paths(details.get('cmdline') or [])
    return details


def listening_ports():
    rows = []
    try:
        conns = psutil.net_connections(kind='tcp')
    except (psutil.AccessDenied, OSError):
        conns = []
    seen = set()
    for conn in conns:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        key = (conn.laddr.port, conn.pid, conn.family)
        if key in seen:
            continue
        seen.add(key)
        rows.append({'port': conn.laddr.port, 'ip': conn.laddr.ip, 'pid': conn.pid,
                     'v6': conn.family == socket.AF_INET6})
    owners = _port_owners()
    for row in rows:
        row['uid'] = owners.get(row['port'])
    merged = {}
    for row in rows:
        k = (row['port'], row['pid'])
        if k in merged:
            merged[k]['ip'] += ', ' + row['ip']
        else:
            merged[k] = dict(row)
    return sorted(merged.values(), key=lambda r: r['port'])


def _port_owners():
    """Listening port -> socket owner uid, readable without root."""
    owners = {}
    for path in ('/proc/net/tcp', '/proc/net/tcp6'):
        try:
            with open(path) as handle:
                next(handle)
                for line in handle:
                    parts = line.split()
                    if len(parts) > 7 and parts[3] == '0A':
                        owners.setdefault(int(parts[1].rsplit(':', 1)[1], 16), int(parts[7]))
        except (OSError, ValueError, StopIteration):
            continue
    return owners


def uid_name(uid):
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, TypeError):
        return '?'


# ---------------------------------------------------------------- actions

def same_process(pid, ctime):
    try:
        return ctime is None or abs(psutil.Process(pid).create_time() - float(ctime)) < 0.05
    except psutil.NoSuchProcess:
        return False


def signal_process(pid, ctime=None, force=False):
    """Send SIGTERM (or SIGKILL); escalate through polkit when we lack permission."""
    pid = int(pid)
    if pid in (0, 1, MY_PID):
        return False, _('This process cannot be closed.')
    if not same_process(pid, ctime):
        return True, _('The process no longer exists.')
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, sig)
        return True, ''
    except ProcessLookupError:
        return True, _('The process no longer exists.')
    except PermissionError:
        ok, _out, err = run(['pkexec', 'kill', '-KILL' if force else '-TERM', str(pid)], timeout=120)
        if ok:
            return True, ''
        return False, _('Permission to close it was not granted.') + ' ' + err.strip()[:200]


def wait_gone(pid, ctime, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not same_process(pid, ctime):
            return True
        time.sleep(0.25)
    return not same_process(pid, ctime)


def terminate(pid, ctime=None, grace=5.0):
    """Ask nicely, then force after `grace` seconds. Returns (ok, message)."""
    ok, message = signal_process(pid, ctime)
    if not ok:
        return ok, message
    if wait_gone(pid, ctime, grace):
        return True, message or _('Process closed.')
    ok, message = signal_process(pid, ctime, force=True)
    if ok and wait_gone(pid, ctime, 3):
        return True, _('The process ignored the close request and was force-closed.')
    return False, message or _('The process is still running.')


def systemctl(action, unit, scope='system'):
    if not re.fullmatch(r'[A-Za-z0-9@._:\\-]+', unit or ''):
        return False, _('Invalid service name.')
    if scope == 'user':
        argv = ['systemctl', '--user', action, unit]
    else:
        argv = ['pkexec', 'systemctl', action, unit]
    ok, out, err = run(argv, timeout=180)
    return ok, (out or err).strip()[:300]


def perform(action):
    """Run one serialized action dict (shared by the app, the watcher CLI and the shell)."""
    kind = action.get('type')
    if kind in ('terminate', 'kill'):
        if kind == 'kill':
            return signal_process(action['pid'], action.get('ctime'), force=True)
        return terminate(action['pid'], action.get('ctime'))
    if kind == 'power':
        return set_power_profile(action['profile'])
    if kind in ('unit-stop', 'unit-start', 'unit-restart'):
        return systemctl(kind.split('-')[1], action['unit'], action.get('scope', 'system'))
    if kind == 'unit-disable':
        return systemctl('disable', action['unit'], action.get('scope', 'system')) \
            if action.get('keep_running') else _disable_now(action)
    if kind == 'unit-enable':
        return systemctl('enable', action['unit'], action.get('scope', 'system'))
    return False, _('Unknown action')


def _disable_now(action):
    unit, scope = action['unit'], action.get('scope', 'system')
    if not re.fullmatch(r'[A-Za-z0-9@._:\\-]+', unit or ''):
        return False, _('Invalid service name.')
    argv = (['systemctl', '--user', 'disable', '--now', unit] if scope == 'user'
            else ['pkexec', 'systemctl', 'disable', '--now', unit])
    ok, out, err = run(argv, timeout=180)
    return ok, (out or err).strip()[:300]


def have(binary):
    return shutil.which(binary) is not None
