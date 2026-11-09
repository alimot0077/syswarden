"""Installed software (apt / snap / flatpak) with usage hints, removal with a dry run, and cleanup."""

import time

from gi.repository import Adw, Gtk

from .. import core, inventory
from ..core import fa, human_ago, human_bytes, sep
from ..i18n import _
from .common import spinner, view_switcher, badge, clear_group, confirm, esc, info, page_scroller, run_async, section_box

FILTERS = [('all', 'All'), ('unused', 'Probably unused'), ('dev', 'Developer tools'), ('gui', 'Desktop apps'),
           ('recent', 'Installed in the last 30 days')]


def _failed_dialog(window, title, message):
    info(window, title, message or _('Cancelled.'))


class SoftwarePage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.loaded = False
        self.apt = []
        self.snaps = []
        self.old_snaps = []
        self.flatpaks = []
        self.cleanup = []

        self.stack = Adw.ViewStack(vexpand=True)
        switcher = view_switcher(self.stack)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_margin_top(14)
        switcher.set_margin_bottom(4)
        self.append(switcher)
        self.append(self.stack)

        # APT
        box = section_box()
        self.apt_group = Adw.PreferencesGroup(
            title=_('Packages you installed'),
            description=_('Only packages you (or an AI agent) installed; Ubuntu’s own packages are not listed. '
                          '“Last used” comes from the last time the program’s files were executed.'))
        tools = Gtk.Box(spacing=8)
        self.apt_search = Gtk.SearchEntry(placeholder_text=_('Search…'), width_chars=16)
        self.apt_search.connect('search-changed', lambda *_a: self._fill_apt())
        self.apt_filter = Gtk.DropDown.new_from_strings([_(label) for _key, label in FILTERS])
        self.apt_filter.connect('notify::selected', lambda *_a: self._fill_apt())
        tools.append(self.apt_search)
        tools.append(self.apt_filter)
        self.apt_group.set_header_suffix(tools)
        self.apt_rows = []
        box.append(self.apt_group)
        self.stack.add_titled_with_icon(page_scroller(box, 1000), 'apt', _('APT packages'), 'package-x-generic-symbolic')

        # Snap / Flatpak
        box = section_box()
        self.snap_group = Adw.PreferencesGroup(
            title=_('Snap apps'), description=_('“Last used” is estimated from each app’s data folder in ~/snap.'))
        self.show_base = Gtk.CheckButton(label=_('Show base snaps'), valign=Gtk.Align.CENTER)
        self.show_base.connect('toggled', lambda *_a: self._fill_snaps())
        self.snap_group.set_header_suffix(self.show_base)
        self.snap_rows = []
        box.append(self.snap_group)
        self.flat_group = Adw.PreferencesGroup(title=_('Flatpak apps'))
        self.flat_rows = []
        box.append(self.flat_group)
        self.stack.add_titled_with_icon(page_scroller(box, 1000), 'snap', _('Snap & Flatpak'), 'view-app-grid-symbolic')

        # Cleanup
        box = section_box()
        self.clean_group = Adw.PreferencesGroup(
            title=_('Clean up'), description=_('Things that can be deleted without harming the system or your projects.'))
        self.clean_rows = []
        box.append(self.clean_group)
        self.stack.add_titled_with_icon(page_scroller(box, 1000), 'clean', _('Clean up'), 'user-trash-symbolic')

        self.loading = Adw.ActionRow(title=_('Scanning installed software… (a few seconds)'))
        self.loading.add_prefix(spinner())
        self.apt_group.add(self.loading)
        self.connect('map', lambda *_a: self.load() if not self.loaded else None)

    def show_cleanup(self):
        self.stack.set_visible_child_name('clean')

    def load(self):
        self.loaded = True

        def gather():
            apt = inventory.list_apt_packages() if core.have('apt-mark') else []
            snaps, old = inventory.list_snaps() if core.have('snap') else ([], [])
            flatpaks = inventory.list_flatpaks() if core.have('flatpak') else []
            cleanup = inventory.cleanup_targets(old)
            return apt, snaps, old, flatpaks, cleanup

        def done(result, error):
            if self.loading.get_parent():
                self.apt_group.remove(self.loading)
            if error:
                self.window.toast(_('Error: {e}').format(e=error))
                return
            self.apt, self.snaps, self.old_snaps, self.flatpaks, self.cleanup = result
            self._fill_apt()
            self._fill_snaps()
            self._fill_cleanup()

        run_async(gather, done)

    # ------------------------------------------------------------ apt

    def _fill_apt(self):
        clear_group(self.apt_group, self.apt_rows)
        key = FILTERS[self.apt_filter.get_selected()][0]
        text = self.apt_search.get_text().strip().lower()
        shown = 0
        for pkg in self.apt:
            if key == 'unused' and not pkg['unused']:
                continue
            if key == 'dev' and not pkg['dev']:
                continue
            if key == 'gui' and not pkg['gui']:
                continue
            if key == 'recent' and not (pkg['installed'] and time.time() - pkg['installed'] < 30 * 86400):
                continue
            if text and text not in pkg['name'].lower() and text not in pkg['summary'].lower():
                continue
            shown += 1
            meta = [human_bytes(pkg['size'])]
            if pkg['installed']:
                meta.append(_('installed {t}').format(t=human_ago(pkg['installed'])))
            if pkg['never_used']:
                meta.append(_('never run'))
            elif pkg['last_used']:
                meta.append(_('last used {t}').format(t=human_ago(pkg['last_used'])))
            sub = pkg['summary'] + '\n' + sep().join(meta)
            if pkg['command']:
                sub += '\n' + _('Installed with: {cmd}').format(cmd=pkg['command'][:110])
            row = Adw.ActionRow(title=esc(pkg['name']), subtitle=esc(sub), subtitle_lines=3)
            if pkg['unused']:
                row.add_suffix(badge(_('Unused'), 'yellow'))
            if pkg['dev']:
                row.add_suffix(badge(_('Dev'), 'blue'))
            if pkg['gui']:
                row.add_suffix(badge(_('Desktop app'), 'green'))
            if pkg['protected']:
                row.add_suffix(badge(_('System'), 'gray'))
            else:
                remove = Gtk.Button(icon_name='user-trash-symbolic', valign=Gtk.Align.CENTER, tooltip_text=_('Remove'))
                remove.add_css_class('flat')
                remove.connect('clicked', lambda _b, p=pkg: self._remove_apt(p))
                row.add_suffix(remove)
            self.apt_group.add(row)
            self.apt_rows.append(row)
        unused = [p for p in self.apt if p['unused']]
        self.apt_group.set_title(_('Packages you installed ({n}) — {u} probably unused ({size})').format(
            n=fa(len(self.apt)), u=fa(len(unused)), size=human_bytes(sum(p['size'] for p in unused))))
        if not shown:
            row = Adw.ActionRow(title=_('Nothing found'))
            self.apt_group.add(row)
            self.apt_rows.append(row)

    def _remove_apt(self, pkg):
        def simulated(result, error):
            if error or not result[0]:
                info(self.window, _('Removal dry run failed'), str(error or result[4]))
                return
            _ok, removed, blocked, freed, _err = result
            if blocked:
                info(self.window, _('This removal is dangerous'),
                     _('Removing {name} would also remove core parts of the system ({parts}). '
                       'SysWarden will not do that for your safety.').format(
                         name=pkg['name'], parts=_(', ').join(blocked[:5])))
                return
            purge = Gtk.CheckButton(label=_('Also delete its configuration (purge)'))
            others = [r for r in removed if r != pkg['name']]
            body = _('These packages will be removed, freeing about {size}:').format(size=human_bytes(freed))
            body += '\n\n' + _(', ').join(removed[:25])
            if len(removed) > 25:
                body += ' ' + _('and {n} more').format(n=fa(len(removed) - 25))
            if len(others) > 8:
                body += '\n\n⚠️ ' + _('Many dependent packages will be removed too; review carefully.')
            confirm(self.window, _('Remove {name}?').format(name=pkg['name']), body, _('Remove'),
                    lambda: self._do_remove(pkg['name'], purge.get_active()), extra=purge)

        run_async(inventory.simulate_removal, simulated, [pkg['name']])

    def _do_remove(self, name, purge):
        self.window.toast(_('Removing {name}… (you will be asked for your password)').format(name=name))

        def done(result, error):
            ok, message = result if result else (False, str(error))
            if ok:
                self.window.toast(_('{name} was removed.').format(name=name))
                self.load()
            else:
                _failed_dialog(self.window, _('Removal failed'), message)

        run_async(inventory.remove_packages, done, [name], purge)

    # ------------------------------------------------------------ snaps & flatpaks

    def _fill_snaps(self):
        clear_group(self.snap_group, self.snap_rows)
        for snap in self.snaps:
            if snap['base'] and not self.show_base.get_active():
                continue
            last = (_('last used {t}').format(t=human_ago(snap['last_used'])) if snap['last_used']
                    else _('never run'))
            row = Adw.ActionRow(title=esc(snap['name']),
                                subtitle=esc(sep().join([snap['version'], human_bytes(snap['size']), last])))
            stale = not snap['base'] and (not snap['last_used'] or time.time() - snap['last_used'] > 90 * 86400)
            if stale:
                row.add_suffix(badge(_('Unused'), 'yellow'))
            if snap['base']:
                row.add_suffix(badge(_('Base'), 'gray'))
            else:
                remove = Gtk.Button(icon_name='user-trash-symbolic', valign=Gtk.Align.CENTER, tooltip_text=_('Remove'))
                remove.add_css_class('flat')
                remove.connect('clicked', lambda _b, s=snap: confirm(
                    self.window, _('Remove snap {name}?').format(name=s['name']),
                    _('The app and its data will be removed (snap keeps an automatic snapshot).'),
                    _('Remove'), lambda: self._run_remove(inventory.remove_snap, s['name'], s['name'])))
                row.add_suffix(remove)
            self.snap_group.add(row)
            self.snap_rows.append(row)
        clear_group(self.flat_group, self.flat_rows)
        for app in self.flatpaks:
            last = (_('last used {t}').format(t=human_ago(app['last_used'])) if app['last_used'] else _('never run'))
            row = Adw.ActionRow(title=esc(app['name']), subtitle=esc(sep().join([app['id'], app['size_text'], last])))
            remove = Gtk.Button(icon_name='user-trash-symbolic', valign=Gtk.Align.CENTER, tooltip_text=_('Remove'))
            remove.add_css_class('flat')
            remove.connect('clicked', lambda _b, a=app: confirm(
                self.window, _('Remove {name}?').format(name=a['name']), _('This Flatpak app will be removed.'),
                _('Remove'), lambda: self._run_remove(inventory.remove_flatpak, a['name'], a['id'], a['installation'])))
            row.add_suffix(remove)
            self.flat_group.add(row)
            self.flat_rows.append(row)
        self.flat_group.set_visible(bool(self.flatpaks))

    def _run_remove(self, func, label, *args):
        self.window.toast(_('Removing {name}…').format(name=label))

        def done(result, error):
            ok, message = result if result else (False, str(error))
            if ok:
                self.window.toast(_('{name} was removed.').format(name=label))
                self.load()
            else:
                _failed_dialog(self.window, _('Removal failed'), message)

        run_async(func, done, *args)

    # ------------------------------------------------------------ cleanup

    def _fill_cleanup(self):
        clear_group(self.clean_group, self.clean_rows)
        total = sum(t['size'] for t in self.cleanup)
        self.clean_group.set_title(_('Clean up — up to {size} can be freed').format(size=human_bytes(total)))
        for target in sorted(self.cleanup, key=lambda t: -t['size']):
            row = Adw.ActionRow(title=esc(target['title']), subtitle=esc(target['detail']), subtitle_lines=2)
            size = Gtk.Label(label=human_bytes(target['size']))
            size.add_css_class('heading')
            size.add_css_class('sg-num')
            row.add_suffix(size)
            if target['root']:
                row.add_suffix(badge(_('Admin'), 'gray'))
            button = Gtk.Button(label=_('Clean'), valign=Gtk.Align.CENTER)
            button.add_css_class('pill')
            button.connect('clicked', lambda _b, t=target: self._clean(t))
            row.add_suffix(button)
            self.clean_group.add(row)
            self.clean_rows.append(row)
        if not self.cleanup:
            row = Adw.ActionRow(title=_('Nothing significant to clean up'))
            self.clean_group.add(row)
            self.clean_rows.append(row)

    def _clean(self, target):
        warning = {
            'cache:trash': _('Files in the trash will be deleted permanently and cannot be recovered.'),
            'journal': _('Old system logs will be deleted; only the newest 200 MB are kept.'),
            'autoremove': _('Packages no other software needs anymore will be removed.'),
            'oldsnaps': _('Only previous (disabled) snap revisions are deleted; the current ones are untouched.'),
        }.get(target['id'], _('These caches are rebuilt automatically when needed (downloaded again).'))

        def go():
            self.window.toast(_('Cleaning {name}…').format(name=target['title']))

            def done(result, error):
                ok, message = result if result else (False, str(error))
                if ok:
                    self.window.toast(_('{name} cleaned; about {size} freed.').format(
                        name=target['title'], size=human_bytes(target['size'])))
                    self.cleanup = [t for t in self.cleanup if t['id'] != target['id']]
                    self._fill_cleanup()
                else:
                    _failed_dialog(self.window, _('Cleanup failed'), message)

            run_async(inventory.clean, done, target)

        confirm(self.window, _('Clean {name}?').format(name=target['title']),
                warning + '\n\n' + _('Size: {size}').format(size=human_bytes(target['size'])), _('Clean'), go)
