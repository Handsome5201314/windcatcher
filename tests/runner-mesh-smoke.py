"""Opt-in real production-runner integration smoke, with no patient data.

Prepare a case with tests/parallel-mesh-smoke.py --export-prepared-case,
then pass the printed directory with --prepared-case. The prepared case is
copied to a fresh .local-monitor directory. No runner or external tool is mocked.
The installed, coarse motorbike tutorial proves workflow only, not nasal CFD
accuracy, mesh sensitivity, convergence, or clinical/design suitability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared-case', type=Path, required=True)
    parser.add_argument('--image', default='openfoam/openfoam11-paraview510:11')
    parser.add_argument('--processes', type=int, default=16)
    parser.add_argument('--reuse-processes', type=int, default=4)
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args()
    prepared = args.prepared_case.resolve()
    if not (prepared / 'cfd/system/controlDict').is_file():
        parser.error('Prepared directory must contain the exported cfd/ case')
    if min(args.processes, args.reuse_processes) < 2 or args.processes == args.reuse_processes:
        parser.error('Two different process counts of at least 2 are required')

    target = APP_ROOT / '.local-monitor' / f'runner-smoke-{uuid.uuid4().hex[:12]}'
    target.mkdir(parents=True)
    os.environ['WINDCATCHER_MONITOR_DIR'] = str(target / 'monitor')
    os.environ['OPENFOAM_IMAGE'] = args.image
    os.environ.pop('OPENFOAM_MPI_PROCS', None)

    import numpy as np
    from pipeline import cases, config, openfoam_runner
    from pipeline.cfd_validation import inspect_latest_fields
    from pipeline.volume import Volume, save_volume

    # These are the normal storage globals, set only in this isolated process.
    # All runner, preview, case storage, backend detection and Docker calls are real.
    config.CASES_DIR = target / 'cases'
    cases.CASES_DIR = config.CASES_DIR
    case_id = cases.create_case('synthetic', 'installed OpenFOAM tutorial', synthetic=True)
    root = cases.case_dir(case_id)
    cfd = root / 'cfd'
    shutil.copytree(prepared / 'cfd', cfd, dirs_exist_ok=True)
    shutil.copytree(prepared / 'segment', root / 'segment', dirs_exist_ok=True)
    save_volume(case_id, Volume(
        array=np.zeros((3, 3, 3), dtype=np.float32),
        spacing=(1, 1, 1), origin=(0, 0, 0),
        axis_dirs=((0, 0, 1), (0, 1, 0), (1, 0, 0)),
        meta={'source': 'non-patient synthetic preview test volume'},
    ))
    evidence = {'success': False, 'case_root': str(root), 'results': []}

    def fingerprint() -> dict:
        mesh = cfd / 'constant/polyMesh'
        return {name: hashlib.sha256((mesh / name).read_bytes()).hexdigest()
                for name in ('points', 'faces', 'owner', 'neighbour', 'boundary')}

    def run(count: int) -> dict:
        meta_path = cfd / 'case_meta.json'
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
        meta['parallel'] = {'profile': 'custom', 'requested_subdomains': count}
        meta_path.write_text(json.dumps(meta, indent=2), encoding='utf-8')
        print(json.dumps({'starting': 'production run_case', 'mpi_processes': count,
                          'case_root': str(root)}), flush=True)
        started = time.monotonic()
        result = openfoam_runner.run_case(case_id, backend='docker', timeout_s=args.timeout)
        (target / f'result-{count}.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        evidence['results'].append(result)
        print(json.dumps({'status': result.get('status'), 'mpi_processes': count,
                          'seconds': round(time.monotonic() - started, 3),
                          'execution': result.get('execution'), 'steps': result.get('steps'),
                          'field_validation': result.get('field_validation')},
                         ensure_ascii=False), flush=True)
        if result.get('status') != 'ran':
            raise AssertionError(f'Production runner failed: {result.get("message")}; evidence: {target}')
        assert all(step['returncode'] == 0 for step in result['steps']), result['steps']
        assert result['mesh_validation']['mesh_ok'], result['mesh_validation']
        fields = inspect_latest_fields(cfd)
        assert fields['complete'] and fields['latest_time'] == '3', fields
        assert all(item['finite'] for item in fields['fields'].values()), fields
        assert list((cfd / 'VTK').rglob('*.vtk')), 'Real foamToVTK did not create VTK files'
        log = (cfd / 'logs/simpleFoam.log').read_text(encoding='utf-8')
        assert re.search(r'^Time = 3s?\s*$', log, re.M), 'Solver did not reach 3 iterations'
        assert result['report_eligible'] is False, 'A short smoke solve must not become a clinical report'
        parts = [p for p in cfd.iterdir() if re.fullmatch(r'processor[0-9]+', p.name)]
        assert len(parts) == count, [p.name for p in parts]
        return result

    try:
        first = run(args.processes)
        first_names = {step['name'] for step in first['steps']}
        assert {'blockMesh', 'decomposeMesh', 'snappyHexMesh', 'reconstructMesh', 'checkMesh'} <= first_names
        assert first['execution']['mesh']['action'] == 'rebuilt'
        first_mesh = fingerprint()
        second = run(args.reuse_processes)
        second_names = {step['name'] for step in second['steps']}
        assert not {'blockMesh', 'decomposeMesh', 'snappyHexMesh', 'reconstructMesh'} & second_names
        assert {'checkMesh', 'decomposePar', 'simpleFoam', 'reconstructPar', 'foamToVTK'} <= second_names
        assert second['execution']['mesh']['action'] == 'reused'
        assert second['execution']['mesh']['requested_subdomains'] == args.processes
        assert second['parallel']['requested_subdomains'] == args.reuse_processes
        assert first_mesh == fingerprint(), 'Reusing and repartitioning changed the root mesh'
        archive = cfd / second['execution']['archive']['path']
        assert (archive / '3/U').is_file() and (archive / '3/p').is_file()
        assert (archive / 'run_status.json').is_file()
        assert (archive / 'VTK').is_dir()
        evidence.update(success=True, mesh_fingerprint=first_mesh,
                        second_archive=str(archive), generation_processes=args.processes,
                        current_solve_processes=args.reuse_processes)
        print(json.dumps({key: value for key, value in evidence.items() if key != 'results'},
                         ensure_ascii=False), flush=True)
    except Exception as exc:
        evidence['failure'] = {'type': type(exc).__name__, 'message': str(exc)}
        raise
    finally:
        (target / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                              encoding='utf-8')


if __name__ == '__main__':
    main()
