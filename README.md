# EntropiaTracker

A desktop tracker for Entropia Universe hunting, loot, skills and professions. Previously named SkillTracker.

## Download and run on Windows

1. Open the [latest release](https://github.com/Bashar-A/SkillTracker/releases/latest).
2. Download **EntropiaTracker-windows-x64.zip** and extract it into a writable folder, such as a folder inside Documents.
3. Run **EntropiaTracker.exe**. Python and an installer are not required.

Keep the executable in its extracted folder. Settings and personal JSON files are saved beside it, including when you launch it through a shortcut. Bundled catalogs are included inside the executable. The release also includes a SHA-256 checksum file.

## Main features

- **Live Monitor** reads chat.log and tracks combat, loot and skill gains with resumable log positions.
- **Loot Tracker** shows loot events, item composition, markup, returns and charts. Exclude unrelated loot and recalculate the session.
- **Hunting Setup** stores weapons, amplifiers, attachments and favorite mob/maturity targets.
- **Previous Sessions / Session Details** browse completed sessions and their events.
- **What Mob to Hunt / Analytic Sessions** compare historical hunting results and select sessions for analysis.
- **Mob Catalog** views, adds and edits mobs and maturity HP/levels.
- **Professions / Skills** calculates profession gains, skill TT and HP projections, and imports current skills.
- **Settings** configures [Mediocre Entropia](https://github.com/Bashar-A/mediocre-entropia) uploads and server-priority mob synchronization. Light/dark themes and two layouts are available.

Choose chat.log in Live Monitor, configure your hunting setup, then start a session and synchronization. Log timestamps are UTC; local display and uploaded session times are handled separately.

## Import current skills

Open **Professions / Skills → Import Skills JSON**, choose a file, and confirm the replacement. The complete file is validated before any changes are saved. Import replaces the current skill snapshot, clears unsaved profession edits, and refreshes profession values, looter levels and current-session projections. Saved session history remains unchanged.

Supported JSON formats:

```json
{
  "Skinning": 1500.5,
  "Butchering": 2200,
  "Intelligence": 45
}
```

The same mapping can be wrapped as `{ "skills": { "Skinning": 1500.5 } }`. Files may contain a UTF-8 BOM. Provide 1–500 unique skill names with numeric values from 0 to 100,000; negative, non-finite, boolean or duplicate values/names are rejected. Imports are limited to 2 MB. The verified skill TT curve currently supports up to 20,000 points; larger historical values remain readable but cannot be projected beyond that curve.

## Upgrade without losing data

Close the old application and back up its folder. Extract the new release into that folder, or copy your personal JSON files and their `.bak` files beside the new executable. Replace the application and README; keep your existing data. Legacy filenames intentionally remain compatible:

| File | Data |
| --- | --- |
| `current_skills.json` | Current skill values |
| `skill_tracker_state.json` | Settings, log position and appearance |
| `skill_tracker_sessions.json` | Saved hunting sessions |
| `mob_analysis_sessions.json` | Analytic session archive |
| `hunting_setups.json`, `favorite_mobs.json` | Saved equipment and targets |
| `mob_catalog.json` | Local mob edits and synchronized catalog |
| `loot_markups.json`, `market_data.json` | Manual and historical markup |
| `server_settings.json`, `server_upload_receipts.json` | Server connection and upload receipts |

An optional remembered API token is stored locally in server_settings.json. Keep that file private. The release contains reference catalogs only, never personal sessions, settings, skills or tokens. Existing Python shortcuts using `skill_tracker_ui.py` continue to work.

## Run from source

Use Python 3.12 with Tkinter. Windows Python from python.org includes Tkinter; on Ubuntu install `python3-tk`.

```sh
git clone https://github.com/Bashar-A/SkillTracker.git EntropiaTracker
cd EntropiaTracker
python entropia_tracker_ui.py
```

The application uses the Python standard library and bundled Python catalogs. No runtime pip dependencies are required. See [server uploads](docs/server-uploads.md) and [history performance](docs/history-performance.md) for details.

## Tests and local Windows build

```sh
python -m unittest discover -s tests -v
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean EntropiaTracker.spec
python scripts/smoke_release.py dist/EntropiaTracker.exe
```

Build the Windows executable on Windows. Desktop tests require a display; Linux CI runs them under Xvfb. The packaged smoke test checks the real GUI, all reference catalogs, startup from an unrelated directory and compatibility with legacy data.

## Automatic releases

Every push to **master**, including a merged pull request, runs the test suite, builds and smoke-tests the Windows x64 executable, then publishes a GitHub Release containing **EntropiaTracker-windows-x64.zip** and its checksum. Releases use unique `build-<run number>` tags tied to the source commit. Pull requests build the same downloadable artifact for verification without publishing a release. A workflow can also be started manually on master. GitHub Actions uses the built-in GITHUB_TOKEN; no extra release secret is needed.
