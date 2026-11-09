"""Startup apps and services (system and user), with boot toggles and start/stop; open ports."""

import time

import psutil
from gi.repository import Adw, Gtk

from .. import core, inventory
from ..core import fa, human_bytes, sep
from ..i18n import _
from .common import spinner, badge, clear_group, confirm, esc, icon_for, page_scroller, run_async, section_box

CATEGORY_BADGE = {'dev': ('Dev', 'yellow'), 'optional': ('Optional', 'blue'), 'essential': ('Essential', 'gray')}


def _failed(message):
    return _('Failed: {e}').format(e=message or _('cancelled'))


class StartupPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.loaded = False
        box = section_box()
        self.append(page_scroller(box))

        self.auto_group = Adw.PreferencesGroup(
            title=_('Startup apps'),
            description=_('Apps that open by themselves after you log in. Turning one off only affects your '
                          'account and can be undone.'))
        self.show_system_auto = Gtk.CheckButton(label=_('Show system entries'), valign=Gtk.Align.CENTER)
        self.show_system_auto.connect('toggled', lambda *_a: self._fill_autostart())
        self.auto_group.set_header_suffix(self.show_system_auto)
        self.auto_rows = []
        box.append(self.auto_group)

        self.sys_group = Adw.PreferencesGroup(
            title=_('System services'),
            description=_('Services started with the computer. Switch = start at boot, button = start/stop right '
                          'now. Changes ask for your administrator password.'))
        self.show_all_sys = Gtk.CheckButton(label=_('Show essential services'), valign=Gtk.Align.CENTER)
        self.show_all_sys.connect('toggled', lambda *_a: self._fill_services(
            self.sys_group, self.sys_rows, self.system_services, self.show_all_sys))
        self.sys_group.set_header_suffix(self.show_all_sys)
        self.sys_rows = []
        box.append(self.sys_group)

        self.user_group = Adw.PreferencesGroup(title=_('User services'),
                                               description=_('Background services running for your account.'))
        self.show_all_user = Gtk.CheckButton(label=_('Show essential services'), valign=Gtk.Align.CENTER)
        self.show_all_user.connect('toggled', lambda *_a: self._fill_services(
            self.user_group, self.user_rows, self.user_services, self.show_all_user))
        self.user_group.set_header_suffix(self.show_all_user)
        self.user_rows = []
        box.append(self.user_group)

        self.autostart = []
        self.system_services = []
        self.user_services = []
        self.spinner_row = Adw.ActionRow(title=_('Loading…'))
        self.spinner_row.add_prefix(spinner())
        self.auto_group.add(self.spinner_row)
        self.connect('map', lambda *_a: self.load() if not self.loaded else None)

    def load(self):
        self.loaded = True

        def gather():
            return inventory.list_autostart(), inventory.list_services(), inventory.list_services(user=True)

        def done(result, error):
            if self.spinner_row.get_parent():
                self.auto_group.remove(self.spinner_row)
            if error:
                self.window.toast(_('Could not read services: {e}').format(e=error))
                return
            self.autostart, self.system_services, self.user_services = result
            self._fill_autostart()
            self._fill_services(self.sys_group, self.sys_rows, self.system_services, self.show_all_sys)
            self._fill_services(self.user_group, self.user_rows, self.user_services, self.show_all_user)

        run_async(gather, done)

    # ------------------------------------------------------------ autostart

    def _fill_autostart(self):
        clear_group(self.auto_group, self.auto_rows)
        for entry in self.autostart:
            if entry['protected'] and not self.show_system_auto.get_active():
                continue
            row = Adw.ActionRow(title=esc(entry['name']), subtitle=esc((entry['comment'] or entry['exec'])[:200]),
                                subtitle_lines=2)
            row.add_prefix(icon_for(entry['icon'], size=32))
            if entry['protected']:
                row.add_suffix(badge(_('System'), 'gray'))
            switch = Gtk.Switch(active=entry['enabled'], valign=Gtk.Align.CENTER, sensitive=not entry['protected'])
            switch.connect('state-set', self._toggle_autostart, entry)
            row.add_suffix(switch)
            row.set_activatable_widget(switch)
            self.auto_group.add(row)
            self.auto_rows.append(row)

    def _toggle_autostart(self, switch, state, entry):
        ok, message = inventory.set_autostart(entry, state)
        if ok:
            entry['enabled'] = state
            switch.set_state(state)
            self.window.toast((_('“{name}” will start automatically.') if state else
                               _('“{name}” will no longer start automatically.')).format(name=entry['name']))
        else:
            switch.set_active(not state)
            self.window.toast(_failed(message))
        return True

    # ------------------------------------------------------------ services

    def _fill_services(self, group, rows, services, show_all):
        clear_group(group, rows)
        for svc in services:
            if svc['category'] == 'essential' and not show_all.get_active():
                continue
            title = svc['unit'].removesuffix('.service')
            state = _('Running') if svc['active'] else _('Stopped')
            if svc['memory']:
                state += sep() + human_bytes(svc['memory'])
            lines = [state + (sep() + '💡 ' + svc['note'] if svc['note'] else '')]
            if svc['description'] and svc['description'] != title:
                lines.append(svc['description'])
            row = Adw.ActionRow(title=esc(title), subtitle=esc('\n'.join(lines)), subtitle_lines=3)
            dot = Gtk.Image(icon_name='media-record-symbolic' if svc['active'] else 'media-playback-stop-symbolic',
                            pixel_size=12)
            dot.add_css_class('success' if svc['active'] else 'dim-label')
            row.add_prefix(dot)
            if svc['category'] in CATEGORY_BADGE:
                label, color = CATEGORY_BADGE[svc['category']]
                row.add_suffix(badge(_(label), color))
            protected = svc['category'] == 'essential'
            run_btn = Gtk.Button(icon_name='media-playback-stop-symbolic' if svc['active'] else 'media-playback-start-symbolic',
                                 tooltip_text=_('Stop now') if svc['active'] else _('Start now'),
                                 valign=Gtk.Align.CENTER, sensitive=not protected)
            run_btn.add_css_class('flat')
            run_btn.connect('clicked', self._start_stop, svc)
            row.add_suffix(run_btn)
            can_toggle = svc['file_state'] in ('enabled', 'disabled') and not protected
            switch = Gtk.Switch(active=svc['enabled'], valign=Gtk.Align.CENTER, sensitive=can_toggle,
                                tooltip_text=_('Start at boot') if can_toggle else
                                _('This service cannot be changed from here'))
            switch.connect('state-set', self._toggle_service, svc)
            row.add_suffix(switch)
            group.add(row)
            rows.append(row)
        if not rows:
            row = Adw.ActionRow(title=_('Nothing to show'))
            group.add(row)
            rows.append(row)

    def _toggle_service(self, switch, state, svc):
        def done(result, error):
            ok, message = result if result else (False, str(error))
            if ok:
                svc['enabled'] = state
                switch.set_state(state)
                self.window.toast((_('{unit} will start at boot.') if state else
                                   _('{unit} will no longer start at boot.')).format(unit=svc['unit']))
            else:
                switch.set_active(not state)
                switch.set_state(not state)
                self.window.toast(_failed(message))

        run_async(core.systemctl, done, 'enable' if state else 'disable', svc['unit'], svc['scope'])
        return True

    def _start_stop(self, _button, svc):
        action = 'stop' if svc['active'] else 'start'

        def go():
            def done(result, error):
                ok, message = result if result else (False, str(error))
                if ok:
                    self.window.toast((_('{unit} stopped.') if action == 'stop' else _('{unit} started.'))
                                      .format(unit=svc['unit']))
                else:
                    self.window.toast(_failed(message))
                self.load()
            run_async(core.systemctl, done, action, svc['unit'], svc['scope'])

        if action == 'stop':
            confirm(self.window, _('Stop {unit}?').format(unit=svc['unit']),
                    (svc['note'] + '\n\n' if svc['note'] else '') + _('You can start it again whenever you need it.'),
                    _('Stop'), go)
        else:
            go()


class PortsPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        box = section_box()
        self.append(page_scroller(box, 900))
        self.group = Adw.PreferencesGroup(
            title=_('Open ports'),
            description=_('Programs waiting for connections on a port. Dev servers you forgot to stop show up here.'))
        refresh = Gtk.Button(icon_name='view-refresh-symbolic', valign=Gtk.Align.CENTER, tooltip_text=_('Refresh'))
        refresh.add_css_class('flat')
        refresh.connect('clicked', lambda *_a: self.load())
        self.group.set_header_suffix(refresh)
        self.rows = []
        box.append(self.group)
        self.connect('map', lambda *_a: self.load())

    def load(self):
        def gather():
            ports = core.listening_ports()
            for row in ports:
                row['proc'] = None
                if row['pid']:
                    try:
                        p = psutil.Process(row['pid'])
                        row['proc'] = {'pid': p.pid, 'ctime': p.create_time(), 'name': p.name(),
                                       'cmd': ' '.join(p.cmdline()), 'display': core.pretty_name(p.name(), p.cmdline()),
                                       'elapsed': time.time() - p.create_time()}
                    except psutil.Error:
                        pass
            return ports

        run_async(gather, self._fill)

    def _fill(self, ports, _error):
        clear_group(self.group, self.rows)
        for data in ports or []:
            proc = data['proc']
            local = data['ip'] in ('127.0.0.1', '::1') or data['ip'].startswith('127.')
            owner = core.uid_name(data.get('uid')) if data.get('uid') is not None else '?'
            title = _('Port {port}').format(port=data['port']) + '  —  ' + (
                proc['display'] if proc else _('system service (user {owner})').format(owner=owner))
            sub = (_('This computer only') if local else _('Reachable from the network')) + f' ({data["ip"]})'
            if proc:
                sub += '\n' + sep().join([f'PID {proc["pid"]}',
                                          _('up for {t}').format(t=core.human_duration(proc['elapsed'])),
                                          proc['cmd'][:100]])
            row = Adw.ActionRow(title=esc(title), subtitle=esc(sub), subtitle_lines=2)
            row.add_prefix(Gtk.Image(icon_name='network-wired-symbolic'))
            if not local:
                row.add_suffix(badge(_('Open to network'), 'yellow'))
            if proc:
                stop = Gtk.Button(label=_('Stop'), valign=Gtk.Align.CENTER)
                stop.add_css_class('flat')
                full = {'pid': proc['pid'], 'ctime': proc['ctime'], 'display': proc['display'],
                        'name': proc['name'], 'uid': core.MY_UID, 'unit': None}
                stop.connect('clicked', lambda _b, p=full: self.window.terminate_procs([p], p['display'],
                                                                                        on_done=self.load))
                row.add_suffix(stop)
            self.group.add(row)
            self.rows.append(row)
        if not self.rows:
            row = Adw.ActionRow(title=_('No open ports found'))
            self.group.add(row)
            self.rows.append(row)
        self.group.set_title(_('Open ports ({n})').format(n=fa(len(ports or []))))
