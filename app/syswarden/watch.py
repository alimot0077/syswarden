"""Low-cost background watcher: samples every 5 s, analyzes every 30 s, writes state to tmpfs.

The GNOME Shell tile reads the state file to show live numbers and to raise notifications.
"""

import os
import signal
import sys
import time

from . import core, i18n, inventory, rules

INTERVAL = 5
ANALYZE_EVERY = 6        # samples -> every 30 s
SLOW_EVERY = 120         # samples -> every 10 min


def _compact_proc(p):
    return {'pid': p['pid'], 'ctime': p['ctime'], 'name': p['display'], 'cpu': round(p['cpu'], 1),
            'rss': p['rss'], 'group': p['group_label']}


def run():
    try:
        os.nice(10)
    except OSError:
        pass
    running = True

    def stop(*_a):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sampler = core.Sampler(history=180)
    issues = []
    slow = {}
    profile = None
    tick = 0
    while running:
        started = time.monotonic()
        try:
            sample = sampler.sample()
            if tick % SLOW_EVERY == 0:
                slow = {'failed_units': rules.failed_units(),
                        'boot_dev_services': inventory.boot_dev_services(),
                        'stale_exe': rules.stale_executables(sample['procs'])}
            if tick % ANALYZE_EVERY == 0:
                i18n.reload()          # follow language changes made in the app
                profile = core.power_profile()
                extra = dict(slow, power_profile=profile, ports=core.listening_ports())
                issues = rules.analyze(sample, sampler, extra)
            m = sample['metrics']
            top = sorted((p for p in sample['procs'] if not p['kernel']), key=lambda p: -p['cpu'])[:5]
            state = {
                'ts': sample['ts'], 'lang': i18n.lang(), 'cpu': round(m['cpu'], 1), 'mem_pct': round(m['mem_pct'], 1),
                'mem_used': m['mem_used'], 'mem_total': m['mem_total'], 'swap_pct': m['swap_pct'],
                'temp': m['temp'], 'nvme': m['temps'].get('nvme'), 'disk_pct': m['disk_pct'],
                'load': m['load'], 'profile': profile, 'battery': m['battery'],
                'score': rules.health_score(issues, m),
                'top': [_compact_proc(p) for p in top],
                'issues': issues,
                'hist': {k: [round(v, 1) for v in sampler.sys_hist[k]] for k in ('cpu', 'mem', 'temp')},
            }
            core.write_json_atomic(core.STATE_FILE, state)
        except Exception as error:  # keep watching; log for journalctl --user -u system-guard-watch
            print('watch error:', repr(error), file=sys.stderr, flush=True)
        tick += 1
        delay = INTERVAL - (time.monotonic() - started)
        end = time.monotonic() + max(0.5, delay)
        while running and time.monotonic() < end:
            time.sleep(min(0.5, end - time.monotonic()) if end > time.monotonic() else 0)
    try:
        os.unlink(core.STATE_FILE)
    except OSError:
        pass
