"""Heuristics that turn a sample into a short list of explained, actionable issues."""

import re
from collections import Counter, defaultdict

from . import core
from .core import fa, human_bytes, human_duration, pct
from .i18n import _

# Tunable thresholds (CPU percentages are per core, like `top`: 100 = one full core).
HOT_CPU_WARN = 85
HOT_CPU_CRIT = 95
IDLE_HOT_TEMP = 75
NVME_WARN = 70
SUSTAINED_CPU_WARN = 60          # % over the last 5 minutes
SUSTAINED_CPU_CRIT = 90          # % over the last 10 minutes
LIFETIME_CPU_WARN = 70           # % averaged over the whole life of a process ...
LIFETIME_MIN_AGE = 15 * 60       # ... that has been running at least this long
GROUP_CPU_WARN = 150             # one app (all its processes) over 5 minutes
PROC_MEM_WARN = max(2 * 1024 ** 3, core.TOTAL_MEM * 0.15)
DEV_SERVER_AGE = 3 * 3600
MANY_SAME = 150

DEV_NAMES = re.compile(
    r'^(mysqld|mariadbd|postgres|postmaster|redis-server|mongod|memcached|httpd|apache2|nginx|'
    r'php-fpm.*|php[\d.]*|node|nodejs|deno|bun|python[\d.]*|ruby[\d.]*|java|beam\.smp|'
    r'elasticsearch|rabbitmq-server|minio|caddy|uvicorn|gunicorn|flask|esbuild|vite|'
    r'dockerd|containerd|qemu-system-.*)$')
DB_NAMES = {'mysqld', 'mariadbd', 'postgres', 'postmaster', 'redis-server', 'mongod'}

SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}


def _issue(issue_id, severity, title, detail, suggestion='', actions=(), **extra):
    return {'id': issue_id, 'severity': severity, 'title': title, 'detail': detail,
            'suggestion': suggestion, 'actions': list(actions), **extra}


def _page(page, label, **extra):
    return {'type': 'page', 'page': page, 'label': label, **extra}


def _terminate_action(proc, label=None):
    if proc.get('unit'):
        return {'type': 'unit-stop', 'unit': proc['unit'], 'scope': proc.get('unit_scope', 'system'),
                'label': _('Stop service {name}').format(name=proc['unit'].removesuffix('.service'))}
    return {'type': 'terminate', 'pid': proc['pid'], 'ctime': proc['ctime'], 'label': label or _('End process')}


def _who(proc):
    name = proc['display']
    if proc.get('group_label') and proc['group'].startswith('app:') and proc['group_label'] != name:
        return _('{name} (from {app})').format(name=name, app=proc['group_label'])
    return name


def analyze(sample, sampler=None, extra=None):
    """Return issues sorted by severity. `extra` may carry slow facts (failed units, boot services)."""
    extra = extra or {}
    procs = sample['procs']
    metrics = sample['metrics']
    by_pid = {p['pid']: p for p in procs}
    issues = []
    flagged = set()

    user_managers = {p['pid'] for p in procs
                     if p['name'] == 'systemd' and ('--user' in p['cmdline'] or p['uid'] == core.MY_UID)}
    reparent_targets = user_managers | {1}

    # 1. Abandoned processes: their creator is gone and their working files were deleted.
    for p in procs:
        if p['uid'] != core.MY_UID or p['pid'] == core.MY_PID or p['kernel']:
            continue
        missing = core.missing_paths(p['cmdline'])
        candidate = missing or p['avg_cpu'] > 5 or DEV_NAMES.match(p['name'])
        if not candidate:
            continue
        cwd_deleted, _exe_deleted, deleted_files = core.deleted_state(p['pid'])
        if not (missing or cwd_deleted or deleted_files >= 10):
            continue
        orphan = p['ppid'] in reparent_targets
        busy = max(p['cpu'], p['avg_cpu'])
        if not orphan and busy < 20:
            continue
        severity = 'critical' if busy >= 50 else 'warning'
        reasons = []
        if missing:
            reasons.append(_('the folder it was started with no longer exists ({path})').format(path=missing[0]))
        if cwd_deleted:
            reasons.append(_('its working folder was deleted'))
        if deleted_files >= 10:
            reasons.append(_('it holds {n} files that were deleted').format(n=fa(deleted_files)))
        detail = (_('Why: {reasons}.').format(reasons=_(', ').join(reasons)) + ' ' +
                  (_('The program that started it has exited, leaving it behind.') + ' ' if orphan else '') +
                  _('It has been running for {age} and now uses {cpu} of one CPU core.').format(
                      age=human_duration(p['elapsed']), cpu=pct(p['cpu'])))
        issues.append(_issue(
            f'abandoned:{p["name"]}:{p["ctime"]:.0f}', severity,
            _('Abandoned process: {who}').format(who=_who(p)), detail,
            _('Its files are already gone, so closing it will not break anything.'),
            [_terminate_action(p)], pid=p['pid'], ctime=p['ctime'], kind='abandoned'))
        flagged.add(p['pid'])

    # 2. Sustained CPU hogs (per process).
    for p in procs:
        if p['pid'] in flagged or p['kernel'] or p['pid'] == core.MY_PID:
            continue
        key = (p['pid'], p['ctime'])
        recent5 = sampler.recent_cpu(key, 300) if sampler else None
        recent10 = sampler.recent_cpu(key, 600) if sampler else None
        lifetime_hot = p['elapsed'] > LIFETIME_MIN_AGE and p['avg_cpu'] > LIFETIME_CPU_WARN and p['cpu'] > 30
        severity = None
        if recent10 is not None and recent10 >= SUSTAINED_CPU_CRIT:
            severity, window = 'critical', recent10
        elif recent5 is not None and recent5 >= SUSTAINED_CPU_WARN:
            severity, window = 'warning', recent5
        elif lifetime_hot:
            severity, window = 'warning', p['avg_cpu']
        if not severity:
            continue
        gui = p['group'].startswith('app:')
        detail = _('Keeps using {cpu} of one CPU core on average ({cputime} of CPU time over {age} of running). '
                   'This heats the CPU and keeps the fan spinning.').format(
                       cpu=pct(window), cputime=human_duration(p['cpu_time']), age=human_duration(p['elapsed']))
        if p.get('unit'):
            suggestion = _('This is a system service: killing it just restarts it, so stop the service instead.')
        elif gui:
            suggestion = _('If you are not using this app right now, close it.')
        else:
            suggestion = _('If you do not know what it is or do not need it, closing it is usually safe.')
        issues.append(_issue(f'cpu:{p["name"]}:{p["ctime"]:.0f}', severity,
                             _('High CPU usage: {who}').format(who=_who(p)), detail, suggestion,
                             [_terminate_action(p)], pid=p['pid'], ctime=p['ctime'], kind='cpu'))
        flagged.add(p['pid'])

    # 3. One app spread over many processes (browsers, Electron apps).
    if sampler:
        groups = defaultdict(list)
        for p in procs:
            groups[p['group']].append(p)
        for group, members in groups.items():
            if len(members) < 3 or any(m['pid'] in flagged for m in members):
                continue
            total = 0.0
            complete = True
            for m in members:
                r = sampler.recent_cpu((m['pid'], m['ctime']), 300)
                if r is None:
                    complete = False
                    break
                total += r
            if complete and total >= GROUP_CPU_WARN:
                label = members[0]['group_label']
                issues.append(_issue(
                    f'group-cpu:{group}', 'warning', _('{app} is using a lot of CPU').format(app=label),
                    _('{n} processes of {app} together used {cpu} CPU over the last 5 minutes.').format(
                        n=fa(len(members)), app=label, cpu=pct(total)),
                    _('Close extra tabs or windows. In a browser, its own Task Manager (Shift+Esc) shows the heavy tab.'),
                    [_page('processes', _('Show processes'), filter=label)], kind='group-cpu'))

    # 4. Memory.
    for p in procs:
        if p['rss'] >= PROC_MEM_WARN and p['pid'] not in flagged:
            issues.append(_issue(f'mem:{p["name"]}:{p["ctime"]:.0f}', 'warning',
                                 _('High memory usage: {who}').format(who=_who(p)),
                                 _('Uses {mem} ({pct}) of RAM.').format(mem=human_bytes(p['rss']), pct=pct(p['mem_pct'])),
                                 _('Close it if you do not need it, before the system starts swapping and slows down.'),
                                 [_terminate_action(p)], pid=p['pid'], ctime=p['ctime'], kind='mem'))
    avail_pct = metrics['mem_available'] * 100 / metrics['mem_total']
    if avail_pct < 8:
        issues.append(_issue('sys-mem', 'critical', _('RAM is almost full'),
                             _('Only {mem} of RAM is free.').format(mem=human_bytes(metrics['mem_available'])),
                             _('Close the heaviest apps on the Processes page.'),
                             [_page('processes', _('Show processes'), sort='mem')], kind='sys'))
    elif metrics['swap_total'] and metrics['swap_pct'] > 60 and avail_pct < 25:
        issues.append(_issue('sys-swap', 'warning', _('The system is swapping'),
                             _('{swap} of swap is in use and free RAM is low; that means slowness and heavy disk activity.').format(
                                 swap=human_bytes(metrics['swap_used'])),
                             _('Close a few heavy apps.'),
                             [_page('processes', _('Show processes'), sort='mem')], kind='sys'))

    # 5. Temperatures.
    temp = metrics['temp']
    temps = metrics['temps']
    if temp:
        crit = min(HOT_CPU_CRIT, (temps.get('cpu_crit') or 100) - 5)
        hist = list(sampler.sys_hist['temp'])[-24:] if sampler else [temp]
        cpu_hist = list(sampler.sys_hist['cpu'])[-24:] if sampler else [metrics['cpu']]
        hist = [h for h in hist if h] or [temp]
        median_temp = sorted(hist)[len(hist) // 2]
        avg_cpu = sum(cpu_hist) / max(1, len(cpu_hist))
        if median_temp >= crit:
            issues.append(_issue('temp-crit', 'critical', _('The CPU is very hot'),
                                 _('CPU temperature is {t}°C, close to the danger limit.').format(t=fa(round(median_temp))),
                                 _('Close heavy apps and make sure the air vents are not blocked.'),
                                 [_page('processes', _('Show top consumers'))], kind='temp'))
        elif median_temp >= HOT_CPU_WARN:
            issues.append(_issue('temp-warn', 'warning', _('The CPU is hot'),
                                 _('CPU temperature is {t}°C.').format(t=fa(round(median_temp))),
                                 _('See which program is keeping the CPU busy.'),
                                 [_page('processes', _('Show top consumers'))], kind='temp'))
        elif len(hist) >= 12 and median_temp >= IDLE_HOT_TEMP and avg_cpu < 20:
            issues.append(_issue('temp-idle', 'info', _('The system is idle but warm'),
                                 _('Total CPU usage is {cpu} but the temperature is {t}°C.').format(
                                     cpu=pct(avg_cpu), t=fa(round(median_temp))),
                                 _('Usually caused by Performance mode, a dusty fan or a soft surface under the laptop.'),
                                 kind='temp'))
    if temps.get('nvme') and temps['nvme'] >= NVME_WARN:
        issues.append(_issue('temp-nvme', 'warning', _('The SSD is hot'),
                             _('NVMe temperature is {t}°C.').format(t=fa(round(temps['nvme']))),
                             _('Check for heavy copies or builds.'), kind='temp'))

    # 6. Power profile.
    if extra.get('power_profile') == 'performance':
        on_battery = metrics.get('battery') and not metrics['battery']['plugged']
        issues.append(_issue(
            'power-performance', 'warning' if on_battery else 'info',
            _('Power mode is set to Performance'),
            (_('The CPU runs at higher clocks in this mode: more heat, a louder fan and a faster-draining battery.')
             if on_battery else _('The CPU runs at higher clocks in this mode: more heat and a louder fan.')),
            _('Balanced is enough for everyday work and coding, and the fan gets quieter.'),
            [{'type': 'power', 'profile': 'balanced', 'label': _('Switch to Balanced')}], kind='power'))

    # 7. Zombies.
    zombies = [p for p in procs if p['status'] == 'zombie']
    if zombies:
        parents = Counter(by_pid[z['ppid']]['display'] if z['ppid'] in by_pid else '?' for z in zombies)
        parent = parents.most_common(1)[0][0]
        issues.append(_issue(f'zombies:{parent}', 'info', _('{n} zombie processes').format(n=fa(len(zombies))),
                             _('Finished processes that their parent ({parent}) has not cleaned up.').format(parent=parent),
                             _('They use no resources; restarting {parent} clears them.').format(parent=parent),
                             kind='zombie'))

    # 8. Leftover dev servers listening on ports.
    ports_by_pid = defaultdict(list)
    for row in extra.get('ports', []):
        if row.get('pid'):
            ports_by_pid[row['pid']].append(row['port'])
    shown = 0
    for p in sorted(procs, key=lambda p: -p['elapsed']):
        if shown >= 5:
            break
        if (p['pid'] in flagged or p['uid'] != core.MY_UID or p['pid'] not in ports_by_pid
                or not DEV_NAMES.match(p['name']) or p['elapsed'] < DEV_SERVER_AGE):
            continue
        ports = _(', ').join(fa(x) for x in sorted(set(ports_by_pid[p['pid']]))[:4])
        issues.append(_issue(f'devserver:{p["name"]}:{p["ctime"]:.0f}', 'info',
                             _('Dev server left running: {who}').format(who=_who(p)),
                             _('Listening on port {ports} for {age}.').format(ports=ports, age=human_duration(p['elapsed'])),
                             _('If you are not working on this project, close it to free RAM and the port.'),
                             [_terminate_action(p)], pid=p['pid'], ctime=p['ctime'], kind='devserver'))
        shown += 1

    # 9. Several copies of the same database server.
    db_roots = [p for p in procs if p['name'] in DB_NAMES
                and not (p['ppid'] in by_pid and by_pid[p['ppid']]['name'] == p['name'])]
    for name, count in Counter(p['name'] for p in db_roots).items():
        if count > 1:
            issues.append(_issue(f'dup-db:{name}', 'info',
                                 _('{n} copies of {name} are running').format(n=fa(count), name=name),
                                 _('Usually one from the system, one from XAMPP/Docker, or a forgotten test instance.'),
                                 _('Check on the Processes page which one you do not need.'),
                                 [_page('processes', _('Show processes'), filter=name)], kind='dup'))

    # 10. Process leaks.
    for name, count in Counter(p['name'] for p in procs if p['uid'] == core.MY_UID).items():
        if count >= MANY_SAME and name not in ('chrome', 'chromium', 'firefox', 'code', 'electron'):
            issues.append(_issue(f'many:{name}', 'warning', _('{n} processes named {name}').format(n=fa(count), name=name),
                                 _('That is unusual: a script may be spawning processes and never closing them.'),
                                 _('Check on the Processes page where they come from.'),
                                 [_page('processes', _('Show processes'), filter=name)], kind='leak'))

    # 11. Load and disk.
    load1, _load5, load15 = metrics['load']
    if load1 > core.NCPU * 1.5 and load15 > core.NCPU:
        issues.append(_issue('load', 'warning', _('The system is under heavy load'),
                             _('Load average is {load} while you have {n} cores.').format(
                                 load=fa(f'{load1:.1f}'), n=fa(core.NCPU)),
                             _('Check heavy background jobs.'), kind='sys'))
    clean = _page('software', _('Clean up'), target='clean')
    if metrics['disk_pct'] >= 95:
        issues.append(_issue('disk-crit', 'critical', _('The disk is almost full'),
                             _('Only {free} left.').format(free=human_bytes(metrics['disk_free'])),
                             _('Use Clean up to remove caches and unused packages.'), [clean], kind='disk'))
    elif metrics['disk_pct'] >= 90:
        issues.append(_issue('disk-warn', 'warning', _('Disk space is low'),
                             _('{free} free.').format(free=human_bytes(metrics['disk_free'])),
                             _('Use Clean up to remove caches and unused packages.'), [clean], kind='disk'))

    # 12. Slow facts gathered less often.
    stale = extra.get('stale_exe') or []
    if stale:
        names = _(', ').join(sorted(set(stale))[:6])
        issues.append(_issue('stale-exe', 'info', _('Apps not restarted after an update'),
                             _('{names} are still running the old, deleted version.').format(names=names),
                             _('Restart them (or the computer) when convenient so the update takes effect.'),
                             kind='stale'))
    failed = extra.get('failed_units') or []
    if failed:
        issues.append(_issue('failed:' + ','.join(sorted(failed)), 'info',
                             _('{n} service(s) stopped with an error').format(n=fa(len(failed))),
                             _(', ').join(failed[:5]),
                             _('Start here if something is not working; otherwise it is safe to ignore.'),
                             [_page('startup', _('Show services'))], kind='failed'))
    heavy = extra.get('boot_dev_services') or []
    if heavy:
        issues.append(_issue('boot-dev:' + ','.join(sorted(heavy)), 'info',
                             _('Development services start with the computer'),
                             _('{names} are always running, even when you are not developing.').format(
                                 names=_(', ').join(heavy)),
                             _('Turn off their autostart and start them only when needed.'),
                             [_page('startup', _('Manage services'))], kind='boot'))

    ignored = core.load_ignored()
    issues = [i for i in issues if i['id'] not in ignored]
    issues.sort(key=lambda i: SEVERITY_ORDER[i['severity']])
    return issues


def stale_executables(procs):
    """Names of our own processes whose binary was replaced by an update."""
    names = []
    for p in procs:
        if p['uid'] != core.MY_UID or p['kernel']:
            continue
        exe = core.readlink(f'/proc/{p["pid"]}/exe') or ''
        if exe.endswith(' (deleted)') and not exe.startswith(('/tmp/', '/memfd:', '/dev/shm')):
            names.append(p['group_label'] if p['group'].startswith('app:') else p['name'])
    return names


def failed_units():
    out = []
    for scope in ([], ['--user']):
        ok, text, _err = core.run(['systemctl', *scope, '--failed', '--no-legend', '--plain', '--type=service'],
                                  timeout=10)
        if ok:
            out += [line.split()[0] for line in text.splitlines() if line.strip()]
    return out


def health_score(issues, metrics):
    """0-100 overall score for the banner."""
    score = 100
    for issue in issues:
        score -= {'critical': 30, 'warning': 12, 'info': 3}[issue['severity']]
    if metrics.get('temp'):
        score -= max(0, (metrics['temp'] - 70)) * 0.8
    score -= max(0, metrics['mem_pct'] - 80) * 0.6
    return int(max(0, min(100, score)))
