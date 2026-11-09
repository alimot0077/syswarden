#!/usr/bin/python3
"""Verify every translatable string has a Persian translation with the same {placeholders}."""
import ast, glob, os, re, sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app')
sys.path.insert(0, ROOT)
import gi  # noqa: E402
gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1')
from syswarden.locale_fa import FA  # noqa: E402
from syswarden import inventory  # noqa: E402
from syswarden.ui import common, overview, processes, software, startup  # noqa: E402

ids = []
for path in sorted(glob.glob(os.path.join(ROOT, 'syswarden', '**', '*.py'), recursive=True)):
    for node in ast.walk(ast.parse(open(path).read())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_'
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            ids.append(node.args[0].value)
ids += [n for _p, n, _c in inventory.SERVICE_NOTES] + [t for _i, t, _p in inventory.USER_CACHES]
ids += [l for _k, l in software.FILTERS] + list(processes.FLAG_LABEL.values())
ids += [v[0] for v in startup.CATEGORY_BADGE.values()] + list(common.SEVERITY_LABEL.values())
ids += list(overview.PROFILE_NAMES.values())
missing = sorted({i for i in ids if i not in FA})
holders = lambda s: sorted(re.findall(r'\{(\w*)\}', s))
mismatch = sorted(i for i in set(ids) if i in FA and holders(i) != holders(FA[i]))
unused = sorted(set(FA) - set(ids))
for i in missing: print('MISSING:', repr(i))
for i in mismatch: print('PLACEHOLDER MISMATCH:', repr(i), '->', repr(FA[i]))
for i in unused: print('unused:', repr(i))
print(f'{len(set(ids))} strings, {len(missing)} missing, {len(mismatch)} mismatched, {len(unused)} unused')
sys.exit(1 if missing or mismatch else 0)
