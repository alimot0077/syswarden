"""Overview: health score, live metric cards, detected issues and top consumers."""

from gi.repository import Adw, Gio, Gtk

from .. import core, rules
from ..core import fa, human_bytes, human_duration, pct, sep
from ..i18n import _
from .common import (SEVERITY_ICON, SEVERITY_LABEL, CoreBars, MetricCard, ScoreRing, badge, clear_group, esc,
                     icon_for, page_scroller, section_box)

PROFILE_NAMES = {'performance': 'Performance', 'balanced': 'Balanced', 'power-saver': 'Power Saver'}


class OverviewPage(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        box = section_box()
        self.append(page_scroller(box))

        # Banner
        self.banner = Gtk.Box(spacing=22)
        self.banner.add_css_class('sg-banner')
        self.banner.add_css_class('good')
        self.ring = ScoreRing()
        self.banner.append(self.ring)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, hexpand=True)
        self.banner_title = Gtk.Label(label=_('Checking your system…'), xalign=0, wrap=True)
        self.banner_title.add_css_class('sg-banner-title')
        self.banner_sub = Gtk.Label(label='', xalign=0, wrap=True)
        self.banner_sub.add_css_class('sg-banner-sub')
        texts.append(self.banner_title)
        texts.append(self.banner_sub)
        chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, margin_top=8, column_spacing=8, row_spacing=6,
                            max_children_per_line=6)
        self.chip_uptime = Gtk.Label()
        self.chip_power = Gtk.Label()
        self.chip_battery = Gtk.Label()
        self.chip_watch = Gtk.Label()
        for chip in (self.chip_uptime, self.chip_power, self.chip_battery, self.chip_watch):
            chip.add_css_class('sg-chip')
            chips.append(chip)
        texts.append(chips)
        self.banner.append(texts)
        box.append(self.banner)

        # Metric cards
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=1, max_children_per_line=4,
                           column_spacing=14, row_spacing=14)
        self.cpu_card = MetricCard(_('CPU'), 'speedometer-symbolic', warn=75, crit=92)
        self.cores = CoreBars()
        self.cpu_card.append(self.cores)
        self.temp_card = MetricCard(_('Temperature'), 'weather-clear-symbolic', maximum=100, warn=80, crit=92)
        self.mem_card = MetricCard(_('Memory'), 'media-flash-symbolic', warn=85, crit=94)
        self.disk_card = MetricCard(_('Disk'), 'drive-harddisk-symbolic', chart=False)
        self.disk_bar = Gtk.LevelBar(min_value=0, max_value=100, margin_top=8)
        self.disk_bar.add_offset_value('low', 80)
        self.disk_bar.add_offset_value('high', 90)
        self.disk_bar.add_offset_value('full', 100)
        self.disk_card.append(self.disk_bar)
        self.swap_bar = Gtk.LevelBar(min_value=0, max_value=100, margin_top=4)
        swap_label = Gtk.Label(label=_('Swap'), xalign=0, margin_top=10)
        swap_label.add_css_class('sg-card-sub')
        self.swap_sub = Gtk.Label(xalign=0)
        self.swap_sub.add_css_class('sg-card-sub')
        self.disk_card.append(swap_label)
        self.disk_card.append(self.swap_bar)
        self.disk_card.append(self.swap_sub)
        for card in (self.cpu_card, self.temp_card, self.mem_card, self.disk_card):
            grid.append(card)
        box.append(grid)

        # Issues
        self.issue_group = Adw.PreferencesGroup(
            title=_('Findings'), description=_('Automatic checks; every finding comes with an explanation and a suggestion.'))
        self.issue_rows = []
        self.issue_ids = None
        box.append(self.issue_group)

        # Top consumers
        self.top_group = Adw.PreferencesGroup(
            title=_('Top consumers right now'),
            description=_('CPU % is per core (100% = one full core).'))
        more = Gtk.Button(label=_('All processes'), valign=Gtk.Align.CENTER)
        more.add_css_class('flat')
        more.connect('clicked', lambda *_a: window.show_page('processes'))
        self.top_group.set_header_suffix(more)
        self.top_rows = []
        for _i in range(6):
            row = Adw.ActionRow(subtitle_lines=1)
            row.icon = icon_for(None, size=24)
            row.add_prefix(row.icon)
            row.cpu = Gtk.Label(width_chars=6, xalign=1)
            row.cpu.add_css_class('sg-num')
            row.cpu.add_css_class('heading')
            row.add_suffix(row.cpu)
            row.kill = Gtk.Button(icon_name='process-stop-symbolic', valign=Gtk.Align.CENTER, tooltip_text=_('End process'))
            row.kill.add_css_class('flat')
            row.kill.connect('clicked', self._kill_top, row)
            row.add_suffix(row.kill)
            row.proc = None
            self.top_group.add(row)
            self.top_rows.append(row)
        box.append(self.top_group)

    # ------------------------------------------------------------ updates

    def update_metrics(self, sample, sampler):
        m = sample['metrics']
        s = sep()
        self.cpu_card.set(pct(m['cpu']),
                          _('Load {load}').format(load=fa(f'{m["load"][0]:.1f}')) + s +
                          _('{n} cores').format(n=fa(core.NCPU)),
                          level='sg-hot' if m['cpu'] > 90 else 'sg-warm' if m['cpu'] > 70 else None)
        self.cpu_card.chart.set_values(sampler.sys_hist['cpu'])
        self.cores.set_values(m['per_cpu'])
        temps = m['temps']
        temp = m['temp']
        if temp:
            parts = []
            if temps.get('nvme'):
                parts.append(_('SSD {t}°').format(t=fa(round(temps['nvme']))))
            if temps.get('wifi'):
                parts.append(_('Wi-Fi {t}°').format(t=fa(round(temps['wifi']))))
            if m['fans']:
                parts.append(_('Fan {rpm} RPM').format(rpm=fa(int(m['fans'][0]))))
            hottest = max(temps['cores']) if temps['cores'] else None
            chip = _('Hottest core {t}°').format(t=fa(round(hottest))) if hottest else ''
            self.temp_card.set(f'{fa(round(temp))}°C', s.join(parts) or _('CPU package'), chip,
                               level='sg-hot' if temp >= 90 else 'sg-warm' if temp >= 80 else None)
            self.temp_card.chart.set_values(sampler.sys_hist['temp'])
        else:
            self.temp_card.set('—', _('No temperature sensor found'))
        self.mem_card.set(pct(m['mem_pct']),
                          _('{used} of {total}').format(used=human_bytes(m['mem_used']), total=human_bytes(m['mem_total']))
                          + s + _('{free} free').format(free=human_bytes(m['mem_available'])),
                          level='sg-hot' if m['mem_pct'] > 93 else 'sg-warm' if m['mem_pct'] > 85 else None)
        self.mem_card.chart.set_values(sampler.sys_hist['mem'])
        self.disk_card.set(pct(m['disk_pct']),
                           _('{free} free of {total}').format(free=human_bytes(m['disk_free']), total=human_bytes(m['disk_total'])),
                           level='sg-hot' if m['disk_pct'] >= 95 else 'sg-warm' if m['disk_pct'] >= 90 else None)
        self.disk_bar.set_value(m['disk_pct'])
        if m['swap_total']:
            self.swap_bar.set_value(m['swap_pct'])
            self.swap_sub.set_label(_('{used} of {total}').format(used=human_bytes(m['swap_used']),
                                                                   total=human_bytes(m['swap_total'])))
        self.chip_uptime.set_label(_('Up for {t}').format(t=human_duration(m['uptime'])))
        if m.get('battery'):
            b = m['battery']
            self.chip_battery.set_label(('🔌 ' if b['plugged'] else '🔋 ') + pct(b['percent']))
            self.chip_battery.set_visible(True)
        else:
            self.chip_battery.set_visible(False)

        top = sorted((p for p in sample['procs'] if not p['kernel']), key=lambda p: -p['cpu'])[:6]
        for row, proc in zip(self.top_rows, top):
            row.proc = proc
            row.set_title(esc(proc['display']))
            sub = _('{mem} RAM').format(mem=human_bytes(proc['rss'])) + s + f'PID {fa(proc["pid"])}'
            if proc['group'].startswith('app:') and proc['group_label'] != proc['display']:
                sub = proc['group_label'] + s + sub
            row.set_subtitle(esc(sub))
            row.cpu.set_label(pct(proc['cpu']))
            icon = proc.get('icon')
            if icon:
                row.icon.set_from_gicon(Gio.Icon.new_for_string(icon))
            else:
                row.icon.set_from_icon_name('application-x-executable-symbolic')

    def update_issues(self, issues, metrics, profile, watcher_on):
        score = rules.health_score(issues, metrics)
        crit = [i for i in issues if i['severity'] == 'critical']
        warn = [i for i in issues if i['severity'] == 'warning']
        for css in ('good', 'warn', 'bad'):
            self.banner.remove_css_class(css)
        if crit:
            self.banner.add_css_class('bad')
            self.banner_title.set_label(_('{n} urgent issue(s) need attention').format(n=fa(len(crit))))
            self.banner_sub.set_label(crit[0]['title'])
        elif warn:
            self.banner.add_css_class('warn')
            self.banner_title.set_label(_('{n} warning(s)').format(n=fa(len(warn))))
            self.banner_sub.set_label(warn[0]['title'])
        else:
            self.banner.add_css_class('good')
            self.banner_title.set_label(_('Your system is in good shape'))
            self.banner_sub.set_label(_('{n} minor tip(s)').format(n=fa(len(issues))) if issues
                                      else _('Nothing suspicious found.'))
        self.ring.set_score(score, fa(score))
        self.chip_power.set_label(_('Power mode: {mode}').format(mode=_(PROFILE_NAMES.get(profile or '', profile or '—'))))
        self.chip_watch.set_label(_('Background guard: {state}').format(state=_('on') if watcher_on else _('off')))

        ids = [i['id'] for i in issues]
        if ids == self.issue_ids:
            return
        self.issue_ids = ids
        clear_group(self.issue_group, self.issue_rows)
        if not issues:
            row = Adw.ActionRow(title=_('All good'),
                                subtitle=_('No abandoned, overheating or runaway processes were found.'))
            row.add_prefix(Gtk.Image(icon_name='emblem-ok-symbolic', css_classes=['success']))
            self.issue_group.add(row)
            self.issue_rows.append(row)
            return
        for issue in issues:
            row = Adw.ActionRow(title=esc(issue['title']),
                                subtitle=esc(issue['detail'] + ('\n💡 ' + issue['suggestion'] if issue['suggestion'] else '')),
                                subtitle_lines=0, title_lines=0)
            icon = Gtk.Image(icon_name=SEVERITY_ICON[issue['severity']], pixel_size=22,
                             valign=Gtk.Align.START, margin_top=12)
            icon.add_css_class('sev-' + issue['severity'])
            row.add_prefix(icon)
            buttons = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
            buttons.append(badge(_(SEVERITY_LABEL[issue['severity']]),
                                 {'critical': 'red', 'warning': 'yellow', 'info': 'blue'}[issue['severity']]))
            for action in issue['actions']:
                button = Gtk.Button(label=action['label'], valign=Gtk.Align.CENTER)
                button.add_css_class('pill')
                if action['type'] in ('terminate', 'unit-stop', 'kill') and issue['severity'] == 'critical':
                    button.add_css_class('destructive-action')
                elif action['type'] != 'page':
                    button.add_css_class('suggested-action')
                button.connect('clicked', lambda _b, a=action, i=issue: self.window.run_action(a, i))
                buttons.append(button)
            ignore = Gtk.Button(icon_name='eye-not-looking-symbolic', valign=Gtk.Align.CENTER,
                                tooltip_text=_('Ignore (do not show again)'))
            ignore.add_css_class('flat')
            ignore.connect('clicked', lambda _b, i=issue: self.window.ignore_issue(i))
            buttons.append(ignore)
            row.add_suffix(buttons)
            self.issue_group.add(row)
            self.issue_rows.append(row)

    def _kill_top(self, _button, row):
        if row.proc:
            self.window.terminate_procs([row.proc], row.proc['display'])
