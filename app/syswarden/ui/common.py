"""Shared widgets and helpers for the System Guard window."""

import math
import threading

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Gsk', '4.0')
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

from ..i18n import _, is_rtl  # noqa: E402

CSS = """
.sg-banner { padding: 22px 26px; border-radius: 20px; }
.sg-banner.good { background: color-mix(in srgb, var(--success-bg-color) 20%, var(--card-bg-color)); }
.sg-banner.warn { background: color-mix(in srgb, var(--warning-bg-color) 24%, var(--card-bg-color)); }
.sg-banner.bad  { background: color-mix(in srgb, var(--error-bg-color) 24%, var(--card-bg-color)); }
.sg-banner-title { font-size: 19pt; font-weight: 800; }
.sg-banner-sub { opacity: .8; }
.sg-score { font-size: 20pt; font-weight: 900; }
.sg-card { padding: 16px 18px; border-radius: 18px; }
.sg-card-title { font-weight: 700; opacity: .7; font-size: 10pt; }
.sg-card-value { font-size: 24pt; font-weight: 800; }
.sg-card-sub { opacity: .65; font-size: 9.5pt; }
.sg-chip { border-radius: 999px; padding: 4px 12px; background: alpha(currentColor, .08); font-size: 9.5pt; font-weight: 600; }
.sg-badge { border-radius: 999px; padding: 1px 9px; font-size: 8.5pt; font-weight: 800; }
.sg-badge.red { background: color-mix(in srgb, var(--error-bg-color) 22%, transparent); color: var(--error-color); }
.sg-badge.yellow { background: color-mix(in srgb, var(--warning-bg-color) 25%, transparent); color: var(--warning-color); }
.sg-badge.blue { background: color-mix(in srgb, var(--accent-bg-color) 20%, transparent); color: var(--accent-color); }
.sg-badge.green { background: color-mix(in srgb, var(--success-bg-color) 20%, transparent); color: var(--success-color); }
.sg-badge.gray { background: alpha(currentColor, .1); }
.sev-critical { color: var(--error-color); }
.sev-warning { color: var(--warning-color); }
.sev-info { color: var(--accent-color); }
.sg-num { font-feature-settings: "tnum"; }
.sg-hot { color: var(--error-color); }
.sg-warm { color: var(--warning-color); }
.sg-section-title { font-size: 13pt; font-weight: 800; }
columnview.sg-procs > listview > row { padding-top: 3px; padding-bottom: 3px; }
.sg-empty { padding: 28px; opacity: .7; }
"""

LEGACY_CSS = """
.sg-banner.good { background: alpha(@success_bg_color, .2); }
.sg-banner.warn { background: alpha(@warning_bg_color, .24); }
.sg-banner.bad  { background: alpha(@error_bg_color, .24); }
.sg-badge.red { background: alpha(@error_bg_color, .22); color: @error_color; }
.sg-badge.yellow { background: alpha(@warning_bg_color, .25); color: @warning_color; }
.sg-badge.blue { background: alpha(@accent_bg_color, .2); color: @accent_color; }
.sg-badge.green { background: alpha(@success_bg_color, .2); color: @success_color; }
.sev-critical, .sg-hot { color: @error_color; }
.sev-warning, .sg-warm { color: @warning_color; }
.sev-info { color: @accent_color; }
"""


SEVERITY_ICON = {'critical': 'dialog-error-symbolic', 'warning': 'dialog-warning-symbolic',
                 'info': 'dialog-information-symbolic'}
SEVERITY_LABEL = {'critical': 'Urgent', 'warning': 'Warning', 'info': 'Tip'}


def spinner():
    """Adw.Spinner on libadwaita 1.6+, Gtk.Spinner before that (Ubuntu 24.04)."""
    if hasattr(Adw, 'Spinner'):
        return Adw.Spinner()
    return Gtk.Spinner(spinning=True)


def view_switcher(stack):
    if hasattr(Adw, 'InlineViewSwitcher'):
        switcher = Adw.InlineViewSwitcher(stack=stack)
        switcher.add_css_class('round')
        return switcher
    return Adw.ViewSwitcher(stack=stack, policy=Adw.ViewSwitcherPolicy.WIDE)


def install_css():
    provider = Gtk.CssProvider()
    modern = (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 16)
    if modern:
        provider.load_from_string(CSS)
    else:
        plain = '\n'.join(line for line in CSS.splitlines() if 'var(' not in line and 'color-mix' not in line)
        provider.load_from_string(plain + LEGACY_CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def run_async(func, callback=None, *args):
    """Run func(*args) on a worker thread; deliver (result, error) to callback on the UI thread."""
    def worker():
        try:
            result, error = func(*args), None
        except Exception as exc:  # surfaced to the user as a toast
            result, error = None, exc
        if callback:
            GLib.idle_add(lambda: (callback(result, error), False)[1])
    threading.Thread(target=worker, daemon=True).start()


RLM = '\u200f'


def esc(text):
    """Markup-escape; in RTL mode force each line right-to-left (embedded English lines otherwise flip)."""
    text = text or ''
    if is_rtl():
        text = '\n'.join(RLM + line for line in text.split('\n'))
    return GLib.markup_escape_text(text)


def badge(text, color='gray'):
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class('sg-badge')
    label.add_css_class(color)
    return label


def icon_for(name, fallback='application-x-executable-symbolic', size=None):
    image = Gtk.Image()
    if name:
        try:
            image.set_from_gicon(Gio.Icon.new_for_string(name))
        except GLib.Error:
            image.set_from_icon_name(fallback)
    else:
        image.set_from_icon_name(fallback)
    if size:
        image.set_pixel_size(size)
    return image


_theme_cache = {}


def theme_has(name):
    if name not in _theme_cache:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        _theme_cache[name] = theme.has_icon(name)
    return _theme_cache[name]


def confirm(parent, heading, body, ok_label, on_ok, destructive=True, extra=None):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response('cancel', _('Cancel'))
    dialog.add_response('ok', ok_label)
    dialog.set_response_appearance('ok', Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                   else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response('cancel')
    dialog.set_close_response('cancel')
    if extra:
        dialog.set_extra_child(extra)
    dialog.connect('response', lambda _d, response: on_ok() if response == 'ok' else None)
    dialog.present(parent)
    return dialog


def info(parent, heading, body):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response('ok', _('OK'))
    dialog.present(parent)


def clear_group(group, rows):
    for row in rows:
        group.remove(row)
    rows.clear()


def accent_rgba():
    try:
        rgba = Adw.StyleManager.get_default().get_accent_color_rgba()
        return rgba.red, rgba.green, rgba.blue
    except AttributeError:
        return 0.21, 0.52, 0.89


def level_rgb(value, warn, crit):
    if value >= crit:
        return 0.88, 0.11, 0.14
    if value >= warn:
        return 0.90, 0.55, 0.05
    return None


def _rgba(r, g, b, a=1.0):
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = r, g, b, a
    return color


def _path(svg):
    return Gsk.Path.parse(svg)


class Sparkline(Gtk.Widget):
    """Filled line chart of the last N values, drawn with GTK snapshots (no cairo needed)."""

    def __init__(self, maximum=100, height=46, color=None, warn=None, crit=None):
        super().__init__(hexpand=True)
        self.set_size_request(-1, height)
        self.values = []
        self.maximum = maximum
        self.color = color
        self.warn, self.crit = warn, crit

    def set_values(self, values):
        self.values = list(values)[-120:]
        self.queue_draw()

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if len(self.values) < 2 or width <= 0:
            return
        last = self.values[-1]
        rgb = (level_rgb(last, self.warn, self.crit) if self.warn else None) or self.color or accent_rgba()
        top = max(self.maximum, max(self.values))
        step = width / (len(self.values) - 1)
        pts = [(i * step, height - 2 - (v / top) * (height - 4)) for i, v in enumerate(self.values)]
        line = 'M %.1f %.1f ' % pts[0] + ' '.join('L %.1f %.1f' % p for p in pts[1:])
        area = line + ' L %.1f %.1f L 0 %.1f Z' % (pts[-1][0], height, height)
        snapshot.append_fill(_path(area), Gsk.FillRule.WINDING, _rgba(*rgb, 0.18))
        stroke = Gsk.Stroke.new(2)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        snapshot.append_stroke(_path(line), stroke, _rgba(*rgb, 0.95))


class CoreBars(Gtk.Widget):
    """One thin bar per logical CPU."""

    def __init__(self):
        super().__init__(hexpand=True)
        self.set_size_request(-1, 26)
        self.values = []

    def set_values(self, values):
        self.values = list(values)
        self.queue_draw()

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        if not self.values or width <= 0:
            return
        n = len(self.values)
        gap = 3
        bar = max(2, (width - gap * (n - 1)) / n)
        fg = self.get_color()
        base = accent_rgba()
        for i, value in enumerate(self.values):
            x = i * (bar + gap)
            snapshot.append_color(_rgba(fg.red, fg.green, fg.blue, 0.10), Graphene.Rect().init(x, 0, bar, height))
            rgb = level_rgb(value, 70, 90) or base
            h = height * min(100, value) / 100
            snapshot.append_color(_rgba(*rgb, 0.9), Graphene.Rect().init(x, height - h, bar, h))


class _Ring(Gtk.Widget):
    def __init__(self, size):
        super().__init__()
        self.set_size_request(size, size)
        self.score = 100

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        radius = min(width, height) / 2 - 6
        cx, cy = width / 2, height / 2
        fg = self.get_color()
        stroke = Gsk.Stroke.new(9)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        circle = 'M %.1f %.1f A %.1f %.1f 0 1 1 %.1f %.1f A %.1f %.1f 0 1 1 %.1f %.1f' % (
            cx, cy - radius, radius, radius, cx, cy + radius, radius, radius, cx, cy - radius)
        snapshot.append_stroke(_path(circle), stroke, _rgba(fg.red, fg.green, fg.blue, 0.12))
        if self.score >= 80:
            rgb = (0.18, 0.69, 0.36)
        elif self.score >= 55:
            rgb = (0.90, 0.55, 0.05)
        else:
            rgb = (0.88, 0.11, 0.14)
        fraction = max(0.02, min(0.999, self.score / 100))
        angle = -math.pi / 2 + 2 * math.pi * fraction
        ex, ey = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        arc = 'M %.1f %.1f A %.1f %.1f 0 %d 1 %.1f %.1f' % (cx, cy - radius, radius, radius,
                                                            1 if fraction > 0.5 else 0, ex, ey)
        snapshot.append_stroke(_path(arc), stroke, _rgba(*rgb))


class ScoreRing(Gtk.Overlay):
    def __init__(self, size=92):
        super().__init__()
        self.area = _Ring(size)
        self.set_child(self.area)
        self.label = Gtk.Label(label='—', halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.label.add_css_class('sg-score')
        self.add_overlay(self.label)

    def set_score(self, score, text):
        self.area.score = score
        self.label.set_label(text)
        self.area.queue_draw()


class MetricCard(Gtk.Box):
    def __init__(self, title, icon, chart=True, maximum=100, warn=None, crit=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class('card')
        self.add_css_class('sg-card')
        head = Gtk.Box(spacing=8)
        head.append(Gtk.Image(icon_name=icon))
        title_label = Gtk.Label(label=title, xalign=0, hexpand=True)
        title_label.add_css_class('sg-card-title')
        head.append(title_label)
        self.chip = Gtk.Label()
        self.chip.add_css_class('sg-card-sub')
        head.append(self.chip)
        self.append(head)
        self.value = Gtk.Label(label='—', xalign=0)
        self.value.add_css_class('sg-card-value')
        self.value.add_css_class('sg-num')
        self.append(self.value)
        self.sub = Gtk.Label(label='', xalign=0, wrap=True)
        self.sub.add_css_class('sg-card-sub')
        self.append(self.sub)
        self.chart = Sparkline(maximum=maximum, warn=warn, crit=crit) if chart else None
        if self.chart:
            self.append(self.chart)

    def set(self, value, sub='', chip='', level=None):
        self.value.set_label(value)
        self.sub.set_label(sub)
        self.chip.set_label(chip)
        for css in ('sg-hot', 'sg-warm'):
            self.value.remove_css_class(css)
        if level:
            self.value.add_css_class(level)


def page_scroller(child, maximum=1100):
    clamp = Adw.Clamp(maximum_size=maximum, tightening_threshold=maximum - 100, child=child)
    return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)


def section_box():
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22, margin_top=22, margin_bottom=28,
                   margin_start=18, margin_end=18)
