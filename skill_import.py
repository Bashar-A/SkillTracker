"""Strict skill snapshot imports: validate everything before changing data."""
import json
import math
from pathlib import Path

MAX_SKILLS_BYTES = 2 * 1024 * 1024

def parse_skill_import(raw):
    if isinstance(raw, dict) and 'skills' in raw:
        raw = raw['skills']
    if not isinstance(raw, dict) or not 1 <= len(raw) <= 500:
        raise ValueError('Provide a JSON object containing 1 to 500 skill names and point values.')
    result, names = {}, set()
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120 or any(ord(c) < 32 for c in name):
            raise ValueError('Skill names must contain 1 to 120 characters without control characters.')
        name = name.strip()
        if name.casefold() in names:
            raise ValueError(f'Duplicate skill name: {name}.')
        names.add(name.casefold())
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f'{name}: skill points must be a number.')
        try:
            points = float(value)
        except OverflowError:
            raise ValueError(f'{name}: skill points must be between 0 and 100,000.') from None
        if not math.isfinite(points) or not 0 <= points <= 100000:
            raise ValueError(f'{name}: skill points must be between 0 and 100,000.')
        result[name] = points
    return result

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key}.')
        result[key] = value
    return result

def read_skill_import(path):
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_SKILLS_BYTES + 1)
    if len(data) > MAX_SKILLS_BYTES:
        raise ValueError('Skills JSON exceeds the 2 MB limit.')
    return parse_skill_import(json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique_object))
