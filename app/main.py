#!/usr/bin/python3
"""SysWarden entry point.

  syswarden                 open the window
  syswarden --page NAME     open on a page (overview, processes, startup, software, ports)
  syswarden --watch         run the background watcher (used by the systemd user service)
  syswarden --do JSON       run one action from a notification (terminate, power, unit-stop…)
  syswarden --check         print the current findings in the terminal
  syswarden --version
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))


def main(argv):
    if '--version' in argv:
        from syswarden import __version__
        print('SysWarden', __version__)
        return 0
    if '--watch' in argv:
        from syswarden import watch
        watch.run()
        return 0
    if '--do' in argv:
        from syswarden import core
        action = json.loads(argv[argv.index('--do') + 1])
        ok, message = core.perform(action)
        print(message)
        return 0 if ok else 1
    if '--check' in argv:
        import time
        from syswarden import core, rules
        sampler = core.Sampler()
        sampler.sample()
        time.sleep(2)
        sample = sampler.sample()
        extra = {'power_profile': core.power_profile(), 'ports': core.listening_ports(),
                 'failed_units': rules.failed_units(), 'stale_exe': rules.stale_executables(sample['procs'])}
        issues = rules.analyze(sample, sampler, extra)
        for issue in issues:
            print(f'[{issue["severity"]}] {issue["title"]}\n    {issue["detail"]}\n    -> {issue["suggestion"]}\n')
        if not issues:
            print('No findings.')
        return 0
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from syswarden.ui.window import Application
    return Application(unique='--screenshot' not in ' '.join(argv)).run(argv)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
