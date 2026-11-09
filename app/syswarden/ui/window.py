"""Main window, application, background sampling and shared actions."""

import os
import threading
import time

from gi.repository import Adw, Gio, GLib, Gtk

from .. import __version__, core, i18n, rules
from ..core import fa
from ..i18n import _
from .common import confirm, esc, info, install_css, run_async
from .overview import OverviewPage
from .processes import ProcessesPage
from .software import SoftwarePage
from .startup import PortsPage, StartupPage

APP_ID = 'io.github.alimot0077.SysWarden'
WEBSITE = 'https://github.com/alimot0077/syswarden'

# Killing these ends the desktop session or breaks the system.
REFUSE = {'gnome-shell', 'gnome-session-binary', 'gnome-session-service', 'Xwayland', 'Xorg',
          'gdm', 'gdm3', 'gdm-wayland-session', 'gdm-session-worker', 'systemd-logind', 'dbus-daemon',
          'dbus-broker', 'dbus-broker-launch', 'init', 'kthreadd', 'polkitd', 'systemd-journald'}


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='SysWarden', default_width=1180, default_height=800)
        self.set_size_request(380, 500)
        self.set_icon_name(APP_ID.removesuffix('.desktop'))
        self.sampler = core.Sampler(history=180)
        self.lock = threading.Lock()
        self.sample = None
        self.issues = []
        self.profile = None
        self.extra_cache = {'ts': 0}
        self.sampling = False
        self.analyzing = False
        self.watcher_on = False
        self._prefill_history()

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.overview = OverviewPage(self)
        self.processes = ProcessesPage(self)
        self.startup = StartupPage(self)
        self.software = SoftwarePage(self)
        self.ports = PortsPage(self)
        for page, name, title, icon in (
                (self.overview, 'overview', _('Overview'), 'speedometer-symbolic'),
                (self.processes, 'processes', _('Processes'), 'view-list-symbolic'),
                (self.startup, 'startup', _('Startup & Services'), 'system-run-symbolic'),
                (self.software, 'software', _('Software'), 'package-x-generic-symbolic'),
                (self.ports, 'ports', _('Ports'), 'network-wired-symbolic')):
            self.stack.add_titled_with_icon(page, name, title, icon)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_('Refresh everything'), 'win.refresh')
        section.append(_('Restore ignored findings'), 'win.reset-ignored')
        menu.append_section(None, section)
        languages = Gio.Menu()
        for code, label in i18n.LANGUAGES.items():
            languages.append(label, f"win.language::{code}")
        menu.append_submenu(_('Language'), languages)
        about = Gio.Menu()
        about.append(_('About SysWarden'), 'win.about')
        menu.append_section(None, about)
        header.pack_end(Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=menu, tooltip_text=_('Menu')))
        bottom = Adw.ViewSwitcherBar(stack=self.stack)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_bottom_bar(bottom)
        self.toasts.set_child(self.stack)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 860sp'))
        breakpoint.add_setter(header, 'show-title', False)
        breakpoint.add_setter(bottom, 'reveal', True)
        self.add_breakpoint(breakpoint)

        for name, callback in (('refresh', self.refresh_all), ('reset-ignored', self._reset_ignored),
                               ('about', self._about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda *_a, cb=callback: cb())
            self.add_action(action)
        lang_action = Gio.SimpleAction.new_stateful('language', GLib.VariantType.new('s'),
                                                    GLib.Variant('s', i18n.lang()))
        lang_action.connect('activate', self._change_language)
        self.add_action(lang_action)

        self._tick()
        self._analyze()
        self._sample_source = GLib.timeout_add_seconds(2, self._tick)
        self._issue_source = GLib.timeout_add_seconds(10, self._analyze)
        self.connect('close-request', self._on_close)

    # ------------------------------------------------------------ loops

    def _prefill_history(self):
        state = core.read_state(max_age=60)
        if state and state.get('hist'):
            for key in ('cpu', 'mem', 'temp'):
                self.sampler.sys_hist[key].extend(state['hist'].get(key, []))

    def _tick(self):
        if self.sampling:
            return True
        self.sampling = True

        def work():
            with self.lock:
                return self.sampler.sample()

        def done(sample, error):
            self.sampling = False
            if error or not sample:
                return
            self.sample = sample
            self.overview.update_metrics(sample, self.sampler)
            self.processes.update(sample)

        run_async(work, done)
        return True

    def _analyze(self):
        if self.analyzing:
            return True
        self.analyzing = True

        def work():
            state = core.read_state(max_age=30)
            self.watcher_on = state is not None
            profile = core.power_profile()
            if state and state.get('lang') == i18n.lang():
                ignored = core.load_ignored()
                return [i for i in state.get('issues', []) if i['id'] not in ignored], profile
            if time.time() - self.extra_cache['ts'] > 60:
                procs = self.sample['procs'] if self.sample else []
                self.extra_cache = {'ts': time.time(), 'failed_units': rules.failed_units(),
                                    'stale_exe': rules.stale_executables(procs)}
            with self.lock:
                sample = self.sample or self.sampler.sample()
                extra = dict(self.extra_cache, power_profile=profile, ports=core.listening_ports())
                return rules.analyze(sample, self.sampler, extra), profile

        def done(result, error):
            self.analyzing = False
            if error:
                print('analysis error:', repr(error))
                return
            if not result or not self.sample:
                return
            self.issues, self.profile = result
            self.overview.update_issues(self.issues, self.sample['metrics'], self.profile, self.watcher_on)
            self.processes.set_issue_flags(self.issues)

        run_async(work, done)
        return True

    def refresh_all(self):
        self._tick()
        self._analyze()
        if self.startup.loaded:
            self.startup.load()
        if self.software.loaded:
            self.software.load()
        self.toast(_('Refreshed'))

    def _on_close(self, *_a):
        GLib.source_remove(self._sample_source)
        GLib.source_remove(self._issue_source)
        return False

    # ------------------------------------------------------------ helpers

    def toast(self, text, button=None, callback=None):
        toast = Adw.Toast(title=esc(text), timeout=4)
        if button:
            toast.set_button_label(button)
            toast.connect('button-clicked', lambda *_a: callback())
        self.toasts.add_toast(toast)

    def show_page(self, name, filter_text=None, sort=None):
        self.stack.set_visible_child_name(name)
        if name == 'processes':
            if filter_text is not None:
                self.processes.set_filter(filter_text)
            if sort:
                self.processes.sort_by(sort)
            if self.sample:
                self.processes.update(self.sample, force=True)
        if name == 'software' and filter_text == 'clean':
            self.software.show_cleanup()

    def ignore_issue(self, issue):
        core.add_ignored(issue['id'])
        self.issues = [i for i in self.issues if i['id'] != issue['id']]
        if self.sample:
            self.overview.update_issues(self.issues, self.sample['metrics'], self.profile, self.watcher_on)

        def undo():
            ids = core.load_ignored()
            ids.discard(issue['id'])
            core.save_ignored(ids)
            self._analyze()

        self.toast(_('Hidden. It will not be shown again.'), _('Undo'), undo)

    def _reset_ignored(self):
        core.save_ignored(set())
        self._analyze()
        self.toast(_('All ignored findings are back.'))

    def _change_language(self, action, value):
        code = value.get_string()
        if code == i18n.lang():
            return
        i18n.save_settings(language=code)
        action.set_state(value)
        self.get_application().rebuild()

    def _about(self):
        about = Adw.AboutDialog(
            application_name='SysWarden', application_icon=APP_ID, version=__version__,
            developer_name='Ali Motlaghian', website=WEBSITE, issue_url=WEBSITE + '/issues',
            license_type=Gtk.License.MIT_X11,
            comments=_('A lightweight task manager and system health guardian for Ubuntu and GNOME: abandoned and '
                       'runaway processes, temperatures, startup apps, services, unused software and cleanup.'))
        about.present(self)

    # ------------------------------------------------------------ actions

    def run_action(self, action, issue=None):
        kind = action['type']
        if kind == 'page':
            self.show_page(action['page'], action.get('target') or action.get('filter'), action.get('sort'))
            return
        if kind in ('terminate', 'kill'):
            proc = None
            if self.sample:
                proc = next((p for p in self.sample['procs'] if p['pid'] == action['pid']), None)
            proc = proc or {'pid': action['pid'], 'ctime': action.get('ctime'), 'name': '', 'uid': None,
                            'display': issue['title'] if issue else str(action['pid']), 'unit': None}
            self.terminate_procs([proc], proc['display'], force=kind == 'kill')
            return
        if kind == 'unit-stop':
            confirm(self, _('Stop service {unit}?').format(unit=action['unit']),
                    _('The service will be stopped. You can start it again from “Startup & Services”.'),
                    _('Stop'), lambda: self._perform(action))
            return
        self._perform(action)

    def _perform(self, action):
        def done(result, error):
            ok, message = result if result else (False, str(error))
            if ok:
                self.toast(message or _('Done.'))
            else:
                self.toast(_('Failed: {e}').format(e=message or _('cancelled')))
            GLib.timeout_add(1200, lambda: (self._analyze(), False)[1])

        run_async(core.perform, done, action)

    def terminate_procs(self, procs, label, force=False, on_done=None):
        if not procs:
            return
        for proc in procs:
            if proc['pid'] in (1, core.MY_PID) or proc.get('kernel') or proc.get('name') in REFUSE \
                    or proc.get('name') == 'systemd':
                info(self, _('SysWarden will not close this process'),
                     _('“{name}” is part of the desktop session or the system core; closing it would log you out '
                       'or break the system.').format(name=proc.get('display') or proc.get('name')))
                return
        units = {p.get('unit') for p in procs if p.get('unit')}
        if len(procs) == 1 and units:
            unit = units.pop()
            proc = procs[0]
            confirm(self, _('“{name}” is a service').format(name=label),
                    _('This process belongs to the service {unit}. If it were only killed, systemd would start it '
                      'again, so the service will be stopped instead.').format(unit=unit),
                    _('Stop service'),
                    lambda: self._perform({'type': 'unit-stop', 'unit': unit,
                                           'scope': proc.get('unit_scope') or 'system'}))
            return
        others = any(p.get('uid') not in (None, core.MY_UID) for p in procs)
        count = len(procs)
        body = (_('{n} processes will be closed.').format(n=fa(count)) + ' ' if count > 1 else '')
        body += (_('Any unsaved work in it will be lost.') if not force else
                 _('The process is killed immediately, without a chance to save.'))
        if others:
            body += '\n\n' + _('It belongs to another user (for example root); your administrator password will be asked.')

        def go():
            def work():
                results = [core.signal_process(p['pid'], p.get('ctime'), force=force) for p in procs]
                if not force:
                    deadline = time.time() + 5
                    alive = list(procs)
                    while alive and time.time() < deadline:
                        alive = [p for p in alive if core.same_process(p['pid'], p.get('ctime'))]
                        time.sleep(0.25)
                    results += [core.signal_process(p['pid'], p.get('ctime'), force=True) for p in alive]
                    time.sleep(0.5)
                failed = [m for ok, m in results if not ok]
                still = [p for p in procs if core.same_process(p['pid'], p.get('ctime'))]
                return failed, still

            def done(result, error):
                if error:
                    self.toast(_('Error: {e}').format(e=error))
                    return
                failed, still = result
                if still:
                    info(self, _('Could not close it'), (failed[0] + '\n\n' if failed else '') +
                         _('If it was started from a sandboxed environment (for example another app’s terminal), '
                           'that environment may block closing it. Try from a normal terminal: kill {pid}').format(
                             pid=still[0]['pid']))
                else:
                    self.toast(_('“{name}” was closed.').format(name=label))
                self._tick()
                GLib.timeout_add(1500, lambda: (self._analyze(), False)[1])
                if on_done:
                    GLib.timeout_add(800, lambda: (on_done(), False)[1])

            run_async(work, done)

        confirm(self, (_('Force kill “{name}”?') if force else _('End “{name}”?')).format(name=label), body,
                _('Force kill') if force else _('End task'), go)

    # ------------------------------------------------------------ screenshots (dev aid)

    def save_screenshots(self, directory, pages, done):
        os.makedirs(directory, exist_ok=True)
        queue = list(pages)

        def snap_next():
            if not queue:
                done()
                return False
            name = queue.pop(0)
            page, _sep, sub = name.partition(':')
            self.show_page(page)
            if sub == 'clean':
                self.software.show_cleanup()
            delay = 4500 if page == 'software' else 2000
            GLib.timeout_add(delay, lambda: (self._snap(directory, name.replace(':', '-')), snap_next())[1])
            return False

        snap_next()

    def _snap(self, directory, name):
        from gi.repository import Graphene
        width, height = self.get_width(), self.get_height()
        paintable = Gtk.WidgetPaintable.new(self)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        node = snapshot.to_node()
        if node is None:
            print(f'screenshot {name}: nothing rendered')
            return
        rect = Graphene.Rect().init(0, 0, width, height)
        self.get_renderer().render_texture(node, rect).save_to_png(os.path.join(directory, f'{name}.png'))


class Application(Adw.Application):
    def __init__(self, unique=True):
        flags = Gio.ApplicationFlags.HANDLES_COMMAND_LINE
        if not unique:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.window = None
        self.add_main_option('page', 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING, 'Page to open', 'PAGE')
        self.add_main_option('screenshot', 0, GLib.OptionFlags.NONE, GLib.OptionArg.STRING,
                             'Save screenshots of every page into DIR (development aid)', 'DIR')

    def do_startup(self):
        Adw.Application.do_startup(self)
        install_css()

    def _apply_direction(self):
        Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL if i18n.is_rtl() else Gtk.TextDirection.LTR)

    def do_activate(self):
        if not self.window:
            self._apply_direction()
            self.window = Window(self)
        self.window.present()

    def rebuild(self):
        """Recreate the window after a language change."""
        i18n.reload()
        old = self.window
        page = old.stack.get_visible_child_name() if old else None
        self.window = None
        self.do_activate()
        if page:
            self.window.show_page(page)
        if old:
            old.destroy()

    def do_command_line(self, command_line):
        options = command_line.get_options_dict().end().unpack()
        self.activate()
        if options.get('page'):
            self.window.show_page(options['page'])
        if options.get('screenshot'):
            pages = ['overview', 'processes', 'startup', 'software:clean', 'ports']
            GLib.timeout_add_seconds(5, lambda: (self.window.save_screenshots(
                options['screenshot'], pages, self.quit), False)[1])
        return 0
