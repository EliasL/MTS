#!/usr/bin/env python3
"""Run local regression suites with bounded execution and full logs."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', action='append', choices=['simulator', 'manager', 'gui', 'plot'])
    parser.add_argument('--build', action='store_true', help='Build simulator and scientific tests first')
    parser.add_argument('--build-dir', type=Path, default=root/'MTS2D/build')
    parser.add_argument('--timeout', type=float, default=120, help='Seconds allowed per build or suite')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    build = args.build_dir.resolve()
    logs = Path(tempfile.mkdtemp(prefix='mts-tests-'))
    env = os.environ | {
        'PYTHONPATH': os.pathsep.join(map(str, [root, root/'MTSManager', root/'MTSPlot'])),
        'PYTHONDONTWRITEBYTECODE': '1', 'MPLBACKEND': 'Agg',
        'MTS_TEST_BINARY': str(build/'MTS2D'),
    }
    env.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir())/'mts-test-matplotlib'))

    def run(name, command, cwd):
        start = time.perf_counter()
        log = logs/(name + '.log')
        try:
            with log.open('w') as stream:
                result = subprocess.run(command, cwd=cwd, env=env, stdout=stream,
                                        stderr=subprocess.STDOUT, timeout=args.timeout)
            passed = result.returncode == 0
        except (subprocess.TimeoutExpired, OSError) as error:
            with log.open('a') as stream:
                stream.write('\n' + str(error))
            passed = False
        print(f'{name}: {"PASS" if passed else "FAIL"} ({time.perf_counter()-start:.2f}s)', flush=True)
        if not passed:
            print('\n'.join(log.read_text(errors='replace').splitlines()[-60:]))
        return passed

    print(f'Logs: {logs}', flush=True)
    if args.build and not run('build', ['cmake', '--build', str(build), '--target',
                                       'MTS2D', 'test_MTS2D', '--parallel', '4'], root):
        return 1
    passed = True
    for suite in args.suite or ['simulator', 'manager', 'gui', 'plot']:
        if suite == 'simulator':
            report = logs/'simulator.xml'
            okay = run(suite, [str(build/'test_MTS2D'), '--reporters=xml',
                               '--out=' + str(report), '--duration'], root)
            if report.exists():
                results = ET.parse(report).getroot()
                print('  Scientific cases:', results.find('OverallResultsTestCases').attrib)
        else:
            cwd = root / {'manager':'MTSManager', 'gui':'.', 'plot':'MTSPlot'}[suite]
            okay = run(suite, [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd)
            summary = (logs/(suite + '.log')).read_text(errors='replace').splitlines()
            print('  ' + '; '.join(line for line in summary if line.startswith(('Ran ', 'OK', 'FAILED'))))
        passed &= okay
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
