"""Task-manager style process list: grouped by app or flat, sortable, searchable."""

import time
from collections import defaultdict

from gi.repository import Adw, Gio, GLib, GObject, Gtk

from .. import core
from ..core import fa, human_ago, human_bytes, human_duration, pct, sep
from ..i18n import _
from .common import esc, badge, run_async, theme_has

FLAG_CSS = {'abandoned': 'red', 'hot': 'yellow', 'mem': 'yellow', 'zombie': 'gray',
            'service': 'blue', 'dev': 'blue', 'root': 'gray'}
FLAG_LABEL = {'abandoned': 'Abandoned', 'hot': 'CPU hog', 'mem': 'Memory hog', 'zombie': 'Zombie',
              'service': 'Service', 'dev': 'Dev server', 'root': 'root'}


class ProcItem(GObject.Object):
    __gtype_name__ = 'SgProcItem'
    name = GObject.Property(type=str, default='')
    detail = GObject.Property(type=str, default='')
    cpu = GObject.Property(type=float, default=0)
    cpu_text = GObject.Property(type=str, default='')
    mem = GObject.Property(type=float, default=0)
    mem_text = GObject.Property(type=str, default='')
    pid_text = GObject.Property(type=str, default='')
    user = GObject.Property(type=str, default='')
    age = GObject.Property(type=float, default=0)
    age_text = GObject.Property(type=str, default='')
    flag = GObject.Property(type=str, default='')
    icon = GObject.Property(type=str, default='')

    def __init__(self, key):
        super().__init__()
        self.key = key
        self.procs = []
        self.search = ''


def _icon_string(proc):
    if proc.get('icon'):
        return proc['icon']
    name = proc['name'].lower()
    if theme_has(name):
        return name
    if proc.get('unit'):
        return 'emblem-system-symbolic'
    return 'application-x-executable-symbolic'


class ProcessesPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.items = {}
        self.grouped = True
        self.flags = {}
        self.last_sample = None

        bar = Gtk.Box(spacing=10, margin_top=12, margin_bottom=10, margin_start=14, margin_end=14)
        self.search = Gtk.SearchEntry(placeholder_text=_('Search name, command or user…'), hexpand=True)
        self.search.connect('search-changed', lambda *_a: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        bar.append(self.search)
        toggle_box = Gtk.Box(css_classes=['linked'])
        self.by_app = Gtk.ToggleButton(label=_('Apps'), active=True)
        self.flat = Gtk.ToggleButton(label=_('All processes'), group=self.by_app)
        self.by_app.connect('toggled', self._mode_changed)
        toggle_box.append(self.by_app)
        toggle_box.append(self.flat)
        bar.append(toggle_box)
        self.mine = Gtk.CheckButton(label=_('Only mine'))
        self.mine.connect('toggled', lambda *_a: self.filter.changed(Gtk.FilterChange.DIFFERENT))
        bar.append(self.mine)
        self.append(bar)

        self.store = Gio.ListStore(item_type=ProcItem)
        self.filter = Gtk.CustomFilter.new(self._match)
        filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.view = Gtk.ColumnView(show_row_separators=False, reorderable=False, hexpand=True, vexpand=True)
        self.view.add_css_class('sg-procs')
        self.view.add_css_class('data-table')
        self.sorted = Gtk.SortListModel(model=filtered, sorter=self.view.get_sorter())
        self.selection = Gtk.SingleSelection(model=self.sorted, autoselect=False, can_unselect=True)
        self.selection.connect('notify::selected-item', self._selection_changed)
        self.view.set_model(self.selection)
        self.view.connect('activate', self._activated)

        self.cpu_col = self._column(_('CPU'), 'cpu_text', 'cpu', numeric=True, width=96, reverse=True)
        self._name_column()
        self.mem_col = self._column(_('Memory'), 'mem_text', 'mem', numeric=True, width=104, reverse=True)
        self.pid_col = self._column('PID', 'pid_text', None, numeric=True, width=90)
        self._column(_('User'), 'user', 'user', width=90)
        self._column(_('Running for'), 'age_text', 'age', width=160, reverse=True)
        self._flag_column()
        self.view.sort_by_column(self.cpu_col, Gtk.SortType.DESCENDING)

        scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True)
        self.append(scroller)

        actions = Gtk.ActionBar()
        self.status = Gtk.Label(xalign=0, ellipsize=3)
        self.status.add_css_class('dim-label')
        actions.pack_start(self.status)
        self.details_btn = Gtk.Button(label=_('Details'))
        self.details_btn.connect('clicked', lambda *_a: self._show_details())
        self.end_btn = Gtk.Button(label=_('End task'))
        self.end_btn.add_css_class('destructive-action')
        self.end_btn.connect('clicked', lambda *_a: self._end_selected(False))
        self.kill_btn = Gtk.Button(label=_('Force kill'))
        self.kill_btn.connect('clicked', lambda *_a: self._end_selected(True))
        for button in (self.kill_btn, self.end_btn, self.details_btn):
            actions.pack_end(button)
        self.append(actions)
        self._selection_changed()

    # ------------------------------------------------------------ columns

    def _column(self, title, text_prop, sort_prop, numeric=False, width=None, reverse=False):
        factory = Gtk.SignalListItemFactory()

        def setup(_f, item):
            label = Gtk.Label(xalign=1 if numeric else 0, ellipsize=3)
            if numeric:
                label.add_css_class('sg-num')
            item.set_child(label)

        def bind(_f, item):
            obj = item.get_item()
            item.binding = obj.bind_property(text_prop, item.get_child(), 'label',
                                             GObject.BindingFlags.SYNC_CREATE)

        def unbind(_f, item):
            if getattr(item, 'binding', None):
                item.binding.unbind()
                item.binding = None

        factory.connect('setup', setup)
        factory.connect('bind', bind)
        factory.connect('unbind', unbind)
        column = Gtk.ColumnViewColumn(title=title, factory=factory, resizable=True)
        if width:
            column.set_fixed_width(width)
        if sort_prop:
            def compare(a, b, _data=None):
                x, y = a.get_property(sort_prop), b.get_property(sort_prop)
                if isinstance(x, str):
                    x, y = x.lower(), y.lower()
                return (x > y) - (x < y)
            column.set_sorter(Gtk.CustomSorter.new(compare))
        self.view.append_column(column)
        return column

    def _name_column(self):
        factory = Gtk.SignalListItemFactory()

        def setup(_f, item):
            box = Gtk.Box(spacing=10)
            image = Gtk.Image(pixel_size=20)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            name = Gtk.Label(xalign=0, ellipsize=3)
            detail = Gtk.Label(xalign=0, ellipsize=3)
            detail.add_css_class('caption')
            detail.add_css_class('dim-label')
            texts.append(name)
            texts.append(detail)
            box.append(image)
            box.append(texts)
            item.set_child(box)

        def bind(_f, item):
            obj = item.get_item()
            box = item.get_child()
            image = box.get_first_child()
            name = image.get_next_sibling().get_first_child()
            detail = name.get_next_sibling()
            item.bindings = [
                obj.bind_property('name', name, 'label', GObject.BindingFlags.SYNC_CREATE),
                obj.bind_property('detail', detail, 'label', GObject.BindingFlags.SYNC_CREATE),
            ]
            try:
                image.set_from_gicon(Gio.Icon.new_for_string(obj.icon))
            except GLib.Error:
                image.set_from_icon_name('application-x-executable-symbolic')

        def unbind(_f, item):
            for binding in getattr(item, 'bindings', []):
                binding.unbind()
            item.bindings = []

        factory.connect('setup', setup)
        factory.connect('bind', bind)
        factory.connect('unbind', unbind)
        column = Gtk.ColumnViewColumn(title=_('Name'), factory=factory, expand=True, resizable=True)
        column.set_sorter(Gtk.CustomSorter.new(
            lambda a, b, _d=None: (a.name.lower() > b.name.lower()) - (a.name.lower() < b.name.lower())))
        self.view.append_column(column)

    def _flag_column(self):
        factory = Gtk.SignalListItemFactory()

        def setup(_f, item):
            item.set_child(Gtk.Box(spacing=4))

        def refresh(obj, _pspec, box):
            child = box.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                box.remove(child)
                child = nxt
            for flag in filter(None, obj.flag.split('|')):
                box.append(badge(_(FLAG_LABEL.get(flag, flag)), FLAG_CSS.get(flag, 'gray')))

        def bind(_f, item):
            obj = item.get_item()
            box = item.get_child()
            refresh(obj, None, box)
            item.handler = obj.connect('notify::flag', refresh, box)

        def unbind(_f, item):
            if getattr(item, 'handler', None):
                item.get_item().disconnect(item.handler)
                item.handler = None

        factory.connect('setup', setup)
        factory.connect('bind', bind)
        factory.connect('unbind', unbind)
        column = Gtk.ColumnViewColumn(title=_('Status'), factory=factory, resizable=True)
        column.set_fixed_width(170)
        self.view.append_column(column)

    # ------------------------------------------------------------ data

    def set_issue_flags(self, issues):
        flags = defaultdict(set)
        for issue in issues:
            if issue.get('pid'):
                label = {'abandoned': 'abandoned', 'cpu': 'hot', 'mem': 'mem',
                         'devserver': 'dev'}.get(issue.get('kind'))
                if label:
                    flags[issue['pid']].add(label)
        self.flags = flags

    def _proc_flags(self, proc):
        flags = set(self.flags.get(proc['pid'], ()))
        if proc['status'] == 'zombie':
            flags.add('zombie')
        if proc.get('unit') and not self.grouped:
            flags.add('service')
        if proc['uid'] == 0 and not self.grouped:
            flags.add('root')
        return flags

    def _mode_changed(self, *_a):
        self.grouped = self.by_app.get_active()
        self.pid_col.set_title(_('Count') if self.grouped else 'PID')
        self.store.remove_all()
        self.items.clear()
        if self.last_sample:
            self.update(self.last_sample, force=True)

    def update(self, sample, force=False):
        self.last_sample = sample
        if not force and not self.get_mapped():
            return
        rows = {}
        for proc in sample['procs']:
            if proc['kernel']:
                continue
            key = ('g:' + proc['group']) if self.grouped else proc['key']
            rows.setdefault(key, []).append(proc)

        for key in [k for k in self.items if k not in rows]:
            item = self.items.pop(key)
            found, position = self.store.find(item)
            if found:
                self.store.remove(position)
        new = []
        for key, procs in rows.items():
            item = self.items.get(key)
            if item is None:
                item = self.items[key] = ProcItem(key)
                first = procs[0]
                item.icon = _icon_string(first)
                new.append(item)
            self._fill(item, procs)
        if new:
            self.store.splice(self.store.get_n_items(), 0, new)
        self.cpu_col.get_sorter().changed(Gtk.SorterChange.DIFFERENT)
        total = len(sample['procs'])
        self.status.set_label(_('{n} processes').format(n=fa(total)) + sep() +
                              (_('{n} apps') if self.grouped else _('{n} rows')).format(n=fa(len(rows))))
        self._selection_changed()

    def _fill(self, item, procs):
        item.procs = procs
        first = procs[0]
        cpu = sum(p['cpu'] for p in procs)
        rss = sum(p['rss'] for p in procs)
        if self.grouped:
            name = first['group_label']
            if first['group'].startswith('name:') or len(procs) == 1:
                name = first['display']
            detail = (_('{n} processes').format(n=fa(len(procs))) if len(procs) > 1 else
                      ' '.join(first['cmdline'])[:120] or first['name'])
            if first.get('unit'):
                detail = _('service {unit}').format(unit=first['unit']) + (sep() + detail if len(procs) > 1 else '')
            pid_text = fa(len(procs)) if len(procs) > 1 else fa(first['pid'])
            age = max(p['elapsed'] for p in procs)
        else:
            name = first['display']
            detail = ' '.join(first['cmdline'])[:140] or first['name']
            pid_text = str(first['pid'])
            age = first['elapsed']
        flags = set()
        for p in procs:
            flags |= self._proc_flags(p)
        users = {p['user'] or '?' for p in procs}
        updates = {
            'name': name, 'detail': detail, 'cpu': cpu, 'cpu_text': pct(cpu) if cpu >= 0.5 else pct(0),
            'mem': float(rss), 'mem_text': human_bytes(rss), 'pid_text': pid_text,
            'user': ', '.join(sorted(users))[:20], 'age': age, 'age_text': human_duration(age),
            'flag': '|'.join(sorted(flags)),
        }
        for prop, value in updates.items():
            if item.get_property(prop) != value:
                item.set_property(prop, value)
        item.search = (name + ' ' + detail + ' ' + ' '.join(p['name'] for p in procs[:20]) + ' ' +
                       item.user + ' ' + ' '.join(str(p['pid']) for p in procs[:20])).lower()

    def _match(self, item, _data=None):
        text = self.search.get_text().strip().lower()
        if self.mine.get_active() and not any(p['uid'] == core.MY_UID for p in item.procs):
            return False
        return not text or text in item.search

    def set_filter(self, text):
        self.search.set_text(text or '')

    def sort_by(self, prop):
        for i in range(self.view.get_columns().get_n_items()):
            column = self.view.get_columns().get_item(i)
            if (prop == 'mem' and column is self.mem_col) or (prop == 'cpu' and column is self.cpu_col):
                self.view.sort_by_column(column, Gtk.SortType.DESCENDING)

    # ------------------------------------------------------------ actions

    def _selected(self):
        return self.selection.get_selected_item()

    def _selection_changed(self, *_a):
        item = self._selected()
        has = item is not None
        for button in (self.details_btn, self.end_btn, self.kill_btn):
            button.set_sensitive(has)
        if has and len(item.procs) > 1:
            self.end_btn.set_label(_('End {n} processes').format(n=fa(len(item.procs))))
        else:
            self.end_btn.set_label(_('End task'))

    def _activated(self, _view, position):
        self.selection.set_selected(position)
        self._show_details()

    def _end_selected(self, force):
        item = self._selected()
        if item:
            self.window.terminate_procs(item.procs, item.name, force=force)

    def _show_details(self):
        item = self._selected()
        if not item:
            return
        if len(item.procs) == 1:
            show_process_dialog(self.window, item.procs[0])
        else:
            show_group_dialog(self.window, item)


# ---------------------------------------------------------------- dialogs

def _row(title, value, selectable=True):
    row = Adw.ActionRow(title=title, subtitle=esc(value or '—'), subtitle_lines=0)
    row.set_subtitle_selectable(selectable)
    row.add_css_class('property')
    return row


def show_process_dialog(window, proc):
    def done(details, _error):
        if details is None:
            window.toast(_('The process no longer exists.'))
            return
        dialog = Adw.Dialog(title=proc['display'], content_width=620, content_height=640)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        general = Adw.PreferencesGroup(title=_('General'))
        general.add(_row(_('Process ID (PID)'), str(details['pid'])))
        general.add(_row(_('Command line'), ' '.join(details.get('cmdline') or []) or details.get('name')))
        general.add(_row(_('Executable'), (details.get('exe') or '—') + ('  ' + _('(deleted or updated)') if details['exe_deleted'] else '')))
        general.add(_row(_('Working folder'), (details.get('cwd') or '—') + ('  ' + _('(deleted!)') if details['cwd_deleted'] else '')))
        general.add(_row(_('User'), details.get('username')))
        general.add(_row(_('Started'), human_ago(details['ctime'])))
        general.add(_row(_('Memory'), human_bytes(details['rss'])))
        general.add(_row(_('Threads / child processes'), _('{t} threads').format(t=fa(details['threads'])) + sep() +
                            _('{c} children').format(c=fa(details['children']))))
        page.add(general)
        origin = Adw.PreferencesGroup(title=_('Where does it come from?'))
        origin.add(_row(_('Parents'), ' ← '.join(details['parents']) or '—'))
        if details.get('app_id'):
            origin.add(_row(_('App'), proc.get('group_label') or details['app_id']))
        if details.get('unit'):
            origin.add(_row(_('systemd service'), details['unit'] + ' (' + (_('user') if details['unit_scope'] == 'user' else _('system')) + ')'))
        if details['ports']:
            origin.add(_row(_('Listening ports'), _(', ').join(fa(p) for p in details['ports'])))
        if details['missing'] or details['deleted_files']:
            origin.add(_row(_('Missing files'), (_(', ').join(details['missing'][:3]) + ' ' if details['missing'] else '') +
                            ('· ' + _('{n} open files were deleted').format(n=fa(details['deleted_files']))
                             if details['deleted_files'] else '')))
        page.add(origin)
        buttons = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=18)
        if details.get('unit'):
            stop = Gtk.Button(label=_('Stop service'))
            stop.add_css_class('pill')
            stop.connect('clicked', lambda *_a: (dialog.close(), window.run_action(
                {'type': 'unit-stop', 'unit': details['unit'], 'scope': details['unit_scope'],
                 'label': _('Stop service')})))
            buttons.append(stop)
        end = Gtk.Button(label=_('End task'))
        end.add_css_class('pill')
        end.add_css_class('destructive-action')
        end.connect('clicked', lambda *_a: (dialog.close(), window.terminate_procs([proc], proc['display'])))
        buttons.append(end)
        kill = Gtk.Button(label=_('Force kill'))
        kill.add_css_class('pill')
        kill.connect('clicked', lambda *_a: (dialog.close(), window.terminate_procs([proc], proc['display'], force=True)))
        buttons.append(kill)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(page)
        page.set_vexpand(True)
        box.append(buttons)
        toolbar.set_content(box)
        dialog.set_child(toolbar)
        dialog.present(window)

    run_async(core.process_details, done, proc['pid'])


def show_group_dialog(window, item):
    dialog = Adw.Dialog(title=item.name, content_width=640, content_height=600)
    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    page = Adw.PreferencesPage()
    group = Adw.PreferencesGroup(title=_('{n} processes').format(n=fa(len(item.procs))),
                                 description=_('Click a row to see its details.'))
    for proc in sorted(item.procs, key=lambda p: -p['cpu']):
        row = Adw.ActionRow(title=esc(proc['display']),
                            subtitle=esc(
                                sep().join([f'PID {proc["pid"]}', pct(proc['cpu']), human_bytes(proc['rss'])]) + sep()
                                + ' '.join(proc['cmdline'])[:90]),
                            activatable=True)
        row.connect('activated', lambda _r, p=proc: show_process_dialog(window, p))
        stop = Gtk.Button(icon_name='process-stop-symbolic', valign=Gtk.Align.CENTER, tooltip_text=_('End task'))
        stop.add_css_class('flat')
        stop.connect('clicked', lambda _b, p=proc: window.terminate_procs([p], p['display']))
        row.add_suffix(stop)
        group.add(row)
    page.add(group)
    toolbar.set_content(page)
    dialog.set_child(toolbar)
    dialog.present(window)
