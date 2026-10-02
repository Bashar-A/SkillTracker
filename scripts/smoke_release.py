"""Launch the real packaged executable with legacy data and a foreign cwd."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

def main():
    executable = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix='entropia-tracker-smoke-') as temporary:
        folder = Path(temporary)
        portable = folder / 'Portable app'; portable.mkdir()
        foreign = folder / 'Unrelated working directory'; foreign.mkdir()
        program = portable / executable.name; shutil.copy2(executable, program)
        skills = {'Skinning': 1234.5, 'Intelligence': 100.0}
        sessions = [{'id': 'legacy-session', 'started_at': '2026-01-01T12:00:00', 'ended_at': '2026-01-01T13:00:00', 'mob': 'Carabok', 'maturity': 'Puny', 'ped_cycled': 10}]
        (portable / 'current_skills.json').write_text(json.dumps(skills), encoding='utf-8')
        session_file = portable / 'skill_tracker_sessions.json'
        session_file.write_text(json.dumps(sessions), encoding='utf-8')
        before = session_file.read_bytes()
        (portable / 'skill_tracker_state.json').write_text(json.dumps({'ui_style': 'Command', 'ui_color_scheme': 'Dark'}), encoding='utf-8')
        report_file = folder / 'smoke-report.json'
        result = subprocess.run([str(program), '--smoke-test', str(report_file)], cwd=foreign, timeout=120)
        report = json.loads(report_file.read_text(encoding='utf-8')) if report_file.exists() else {}
        assert result.returncode == 0 and report.get('ok'), report or f'Executable exited {result.returncode} without a report.'
        assert report['name'] == 'EntropiaTracker', report
        assert Path(report['dataDirectory']).resolve() == portable.resolve(), report
        assert report['skills'] == skills and report['sessionIds'] == ['legacy-session'], report
        assert report['hpSkills'] > 0 and report['mobs'] >= 830, report
        assert session_file.read_bytes() == before, 'Packaged startup modified archived sessions.'
        assert not list(foreign.iterdir()), 'User data was written to the unrelated working directory.'
        print('Packaged GUI, bundled reference data, portable paths and legacy data verified.')

if __name__ == '__main__':
    main()
