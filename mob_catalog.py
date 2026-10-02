"""Validated local mob catalog and server-priority merging."""
import copy
import json
import os
import tempfile
from pathlib import Path
from server_sync import UploadError, number

MOB_CATALOG_FILE = Path('mob_catalog.json')

def text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or any(ord(c) < 32 for c in value):
        raise UploadError(f'Invalid {label}.')
    return value.strip()

def mob_rows(raw):
    if isinstance(raw, dict):
        raw = [dict(data, name=name) for name, data in raw.items()]
    if not isinstance(raw, list):
        raise UploadError('Mob catalog must be a list or name-to-details object.')
    result, names = [], set()
    for row in raw:
        if not isinstance(row, dict):
            raise UploadError('Invalid mob record.')
        name = text(row.get('name'), 'mob name', 160)
        key = name.upper()
        if key in names:
            raise UploadError('Duplicate mob name.')
        names.add(key)
        kind = row.get('type') or 'Unknown'
        if kind not in ('Animal', 'Robot', 'Mutant', 'Asteroid', 'Unknown'):
            raise UploadError('Invalid mob type.')
        planets, maturities = row.get('planets', []), row.get('maturities', {})
        if not isinstance(planets, list) or len(planets) > 20 or not isinstance(maturities, dict) or len(maturities) > 200:
            raise UploadError('Provide at most 20 planets and 200 maturities.')
        normalized, seen = {}, set()
        for maturity, data in maturities.items():
            maturity = text(maturity, 'maturity', 100)
            if maturity.upper() in seen or not isinstance(data, dict):
                raise UploadError('Invalid or duplicate maturity.')
            seen.add(maturity.upper())
            normalized[maturity] = {k: None if data.get(k) is None else number(data[k], k, 0, 1e9 if k == 'hp' else 100000) for k in ('hp', 'level')}
        result.append(dict(name=name, type=kind, planets=list(dict.fromkeys(text(p, 'planet', 100) for p in planets)), maturities=normalized))
    return result

def merge_mobs(local, server):
    """A duplicate takes the complete server record, including null HP/level."""
    rows = {r['name'].upper(): r for r in mob_rows(local)}
    rows.update({r['name'].upper(): r for r in mob_rows(server)})
    return {r['name']: {k: copy.deepcopy(r[k]) for k in ('type', 'planets', 'maturities')} for r in sorted(rows.values(), key=lambda r: r['name'].upper())}

def save_mobs(catalog, path=None):
    path = Path(path or MOB_CATALOG_FILE)
    validated = merge_mobs({}, catalog)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as f:
            json.dump(validated, f, ensure_ascii=False, allow_nan=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def load_mobs(defaults, path=None):
    path = Path(path or MOB_CATALOG_FILE)
    if not path.exists(): return merge_mobs({}, defaults)
    return merge_mobs(defaults, json.loads(path.read_text(encoding='utf-8-sig')))
