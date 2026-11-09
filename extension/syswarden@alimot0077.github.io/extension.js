// SysWarden — Quick Settings tile for GNOME Shell 46+.
// Reads the state written by the SysWarden background watcher (tmpfs, every 5 s), shows live
// CPU / temperature, lists findings and top processes, and raises notifications with one-click fixes.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as QuickSettings from 'resource:///org/gnome/shell/ui/quickSettings.js';

const APP_ID = 'io.github.alimot0077.SysWarden.desktop';
const HOME = GLib.get_home_dir();
const MAIN_PY = GLib.build_filenamev([HOME, '.local', 'share', 'syswarden', 'app', 'main.py']);
const SETTINGS = GLib.build_filenamev([HOME, '.config', 'syswarden', 'settings.json']);
const IGNORED = GLib.build_filenamev([HOME, '.config', 'syswarden', 'ignored.json']);
const STATE = GLib.build_filenamev([GLib.get_user_runtime_dir(), 'syswarden', 'state.json']);
const ICON = 'utilities-system-monitor-symbolic';
const SEVERITY_ICON = {critical: 'dialog-error-symbolic', warning: 'dialog-warning-symbolic', info: 'dialog-information-symbolic'};

const FA = {
    'Guard is off': 'نگهبان خاموش است',
    'Start background guard': 'روشن کردن نگهبان پس‌زمینه',
    'Open SysWarden': 'باز کردن SysWarden',
    'Findings': 'موارد شناسایی‌شده',
    'Top processes': 'پرمصرف‌ترین پروسه‌ها',
    'No findings — all good': 'مشکلی پیدا نشد — همه چیز مرتب است',
    '{n} issue(s)': '{n} مورد',
    'Health {score}/100': 'سلامت {score} از ۱۰۰',
    'Open': 'بررسی',
    'Ignore': 'نادیده بگیر',
    'Done': 'انجام شد',
    'Failed': 'انجام نشد',
    'CPU': 'پردازنده',
    'RAM': 'رم',
    '…and {n} more': '…و {n} مورد دیگر',
};

function readJson(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        if (ok)
            return JSON.parse(new TextDecoder().decode(bytes));
    } catch (_e) {
        // missing or half-written file
    }
    return null;
}

function language() {
    const settings = readJson(SETTINGS) || {};
    const choice = settings.language || 'auto';
    if (choice === 'en' || choice === 'fa')
        return choice;
    return (GLib.getenv('LANG') || '').startsWith('fa') ? 'fa' : 'en';
}

function digits(text, lang) {
    return lang === 'fa' ? String(text).replace(/[0-9]/g, d => '۰۱۲۳۴۵۶۷۸۹'[d]).replace(/%/g, '٪') : String(text);
}

function run(argv, callback) {
    try {
        const proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE);
        proc.communicate_utf8_async(null, null, (p, res) => {
            let out = '';
            try {
                [, out] = p.communicate_utf8_finish(res);
            } catch (e) {
                out = String(e);
            }
            callback?.(p.get_successful(), (out || '').trim());
        });
    } catch (e) {
        callback?.(false, String(e));
    }
}

function openApp(page = null) {
    const app = Shell.AppSystem.get_default().lookup_app(APP_ID);
    if (app) {
        if (page)
            app.launch_action(page, global.get_current_time(), -1);
        else
            app.activate();
        return;
    }
    run(['/usr/bin/python3', MAIN_PY, ...(page ? ['--page', page] : [])]);
}

const WardenToggle = GObject.registerClass(
class WardenToggle extends QuickSettings.QuickMenuToggle {
    _init(extension) {
        super._init({title: 'SysWarden', subtitle: '…', iconName: ICON, toggleMode: false});
        this._ext = extension;
        this.connect('clicked', () => {
            Main.panel.closeQuickSettings();
            openApp();
        });
        this.menu.setHeader(ICON, 'SysWarden', '');
        this._metrics = new PopupMenu.PopupMenuItem('', {reactive: false});
        this.menu.addMenuItem(this._metrics);
        this._issuesSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._issuesSection);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._topSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._topSection);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._startItem = this.menu.addAction('', () => {
            run(['systemctl', '--user', 'start', 'syswarden-watch.service']);
        });
        this._openItem = this.menu.addAction('', () => {
            Main.panel.closeQuickSettings();
            openApp();
        });
        this._lastKey = '';
    }

    t(text) {
        return this._lang === 'fa' ? (FA[text] ?? text) : text;
    }

    update(state, lang) {
        this._lang = lang;
        const d = x => digits(x, lang);
        this._startItem.label.text = this.t('Start background guard');
        this._openItem.label.text = this.t('Open SysWarden');
        if (!state) {
            this.subtitle = this.t('Guard is off');
            this.checked = false;
            this._startItem.visible = true;
            this._metrics.label.text = this.t('Guard is off');
            this.menu.setHeader(ICON, 'SysWarden', this.t('Guard is off'));
            this._issuesSection.removeAll();
            this._topSection.removeAll();
            this._lastKey = '';
            return;
        }
        this._startItem.visible = false;
        const issues = (state.issues || []).filter(i => i.severity !== 'info');
        const temp = state.temp ? ` · ${d(Math.round(state.temp))}°C` : '';
        this.subtitle = issues.length
            ? this.t('{n} issue(s)').replace('{n}', d(issues.length))
            : `${this.t('CPU')} ${d(Math.round(state.cpu))}%${temp}`;
        this.checked = issues.length > 0;
        this.menu.setHeader(issues.some(i => i.severity === 'critical') ? SEVERITY_ICON.critical : ICON,
            'SysWarden', this.t('Health {score}/100').replace('{score}', d(state.score ?? 100)));
        this._metrics.label.text = `${this.t('CPU')} ${d(Math.round(state.cpu))}%   ${this.t('RAM')} ${d(Math.round(state.mem_pct))}%` +
            (state.temp ? `   ${d(Math.round(state.temp))}°C` : '');

        const key = JSON.stringify([(state.issues || []).map(i => i.id), (state.top || []).map(p => [p.name, Math.round(p.cpu)]), lang]);
        if (key === this._lastKey)
            return;
        this._lastKey = key;

        this._issuesSection.removeAll();
        const all = state.issues || [];
        if (!all.length) {
            this._issuesSection.addMenuItem(new PopupMenu.PopupMenuItem(this.t('No findings — all good'), {reactive: false}));
        } else {
            for (const issue of all.slice(0, 4)) {
                const item = new PopupMenu.PopupImageMenuItem(issue.title, SEVERITY_ICON[issue.severity]);
                item.label.clutter_text.ellipsize = 3;
                item.label.style = 'max-width: 330px;';
                item.connect('activate', () => {
                    Main.panel.closeQuickSettings();
                    openApp();
                });
                this._issuesSection.addMenuItem(item);
            }
            if (all.length > 4)
                this._issuesSection.addMenuItem(new PopupMenu.PopupMenuItem(
                    this.t('…and {n} more').replace('{n}', d(all.length - 4)), {reactive: false}));
        }

        this._topSection.removeAll();
        const header = new PopupMenu.PopupMenuItem(this.t('Top processes'), {reactive: false});
        header.label.style = 'font-weight: bold; opacity: .7;';
        this._topSection.addMenuItem(header);
        for (const proc of (state.top || []).slice(0, 5)) {
            const item = new PopupMenu.PopupMenuItem(`${d(Math.round(proc.cpu))}%   ${proc.name}`);
            item.label.clutter_text.ellipsize = 3;
            item.label.style = 'max-width: 330px;';
            item.connect('activate', () => {
                Main.panel.closeQuickSettings();
                openApp('processes');
            });
            this._topSection.addMenuItem(item);
        }
    }
});

const WardenIndicator = GObject.registerClass(
class WardenIndicator extends QuickSettings.SystemIndicator {
    _init(extension) {
        super._init();
        this._ext = extension;
        this._icon = this._addIndicator();
        this._icon.icon_name = SEVERITY_ICON.warning;
        this._icon.visible = false;
        this._toggle = new WardenToggle(extension);
        this.quickSettingsItems.push(this._toggle);
        this._notified = new Set();
        this._source = null;
        this._refresh();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 3, () => {
            this._refresh();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _refresh() {
        let state = readJson(STATE);
        if (state && Date.now() / 1000 - (state.ts || 0) > 30)
            state = null;
        const lang = language();
        this._toggle.update(state, lang);
        const serious = state ? (state.issues || []).filter(i => i.severity !== 'info') : [];
        this._icon.visible = serious.length > 0;
        this._icon.icon_name = serious.some(i => i.severity === 'critical') ? SEVERITY_ICON.critical : SEVERITY_ICON.warning;
        for (const issue of serious) {
            if (!this._notified.has(issue.id)) {
                this._notified.add(issue.id);
                this._notify(issue, lang);
            }
        }
    }

    _getSource() {
        if (!this._source) {
            this._source = new MessageTray.Source({title: 'SysWarden', iconName: ICON});
            this._source.connect('destroy', () => {
                this._source = null;
            });
            Main.messageTray.add(this._source);
        }
        return this._source;
    }

    _notify(issue, lang) {
        const t = text => (lang === 'fa' ? FA[text] ?? text : text);
        const source = this._getSource();
        const notification = new MessageTray.Notification({
            source,
            title: issue.title,
            body: issue.detail + (issue.suggestion ? `\n${issue.suggestion}` : ''),
            iconName: SEVERITY_ICON[issue.severity],
            urgency: issue.severity === 'critical' ? MessageTray.Urgency.CRITICAL : MessageTray.Urgency.HIGH,
        });
        const fix = (issue.actions || []).find(a => a.type !== 'page');
        if (fix) {
            notification.addAction(fix.label, () => {
                run(['/usr/bin/python3', MAIN_PY, '--do', JSON.stringify(fix)], (ok, out) => {
                    const done = new MessageTray.Notification({
                        source: this._getSource(),
                        title: ok ? t('Done') : t('Failed'),
                        body: out || fix.label,
                    });
                    this._getSource().addNotification(done);
                });
            });
        }
        notification.addAction(t('Open'), () => openApp());
        notification.addAction(t('Ignore'), () => {
            const ids = new Set(readJson(IGNORED) || []);
            ids.add(issue.id);
            try {
                GLib.mkdir_with_parents(GLib.path_get_dirname(IGNORED), 0o755);
                GLib.file_set_contents(IGNORED, JSON.stringify([...ids]));
            } catch (_e) {
                // not fatal
            }
        });
        source.addNotification(notification);
    }

    destroy() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = 0;
        }
        this._source?.destroy();
        this._source = null;
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class SysWardenExtension extends Extension {
    enable() {
        this._indicator = new WardenIndicator(this);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
