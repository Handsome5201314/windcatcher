"""Opt-in real OpenFOAM 11 smoke test; never mounts or touches patient cases.

Run: python tests/parallel-mesh-smoke.py --processes 16 --reuse-processes 4
The image must already exist locally. A temporary container is always removed.
This small, intentionally coarse tutorial checks the parallel workflow, not
nasal geometry accuracy, clinical results, or large-mesh performance.
Use --export-prepared-case for a static, local case suitable for runner/cache
integration checks; the exported case remains unmeshed and has no patient data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import uuid


SETUP = r'''
from pathlib import Path
import shutil

case = Path('/tmp/windcatcher-parallel-smoke')
shutil.copytree('/opt/openfoam11/tutorials/incompressibleFluid/motorBikeSteady', case)
shutil.copyfile(case / '0/U.orig', case / '0/U')
shutil.copyfile('/opt/openfoam11/tutorials/resources/geometry/motorBike.obj.gz',
                case / 'constant/geometry/motorBike.obj.gz')

# Use the installed tutorial's solver, boundary fields and base block mesh.
# Use the image's built-in surface at a deliberately coarse refinement level.
snappy = ''' + "'''" + r'''
FoamFile { format ascii; class dictionary; object snappyHexMeshDict; }
#includeEtc "caseDicts/mesh/generation/snappyHexMeshDict.cfg"
castellatedMesh true;
snap true;
addLayers false;
geometry
{
    motorBike
    {
        type triSurfaceMesh;
        file "motorBike.obj";
    }
}
castellatedMeshControls
{
    maxGlobalCells 100000;
    refinementSurfaces
    {
        motorBike
        {
            level (2 2);
            patchInfo { type wall; inGroups (motorBikeGroup); }
        }
    }
    refinementRegions {}
    insidePoint (3.0001 3.0001 0.43);
}
snapControls { nSolveIter 30; nFeatureSnapIter 0; }
''' + "'''" + r'''
(case / 'system/snappyHexMeshDict').write_text(snappy)

control = (case / 'system/controlDict').read_text()
control = control[:control.index('functions')]
control = control.replace('endTime         500;', 'endTime         3;')
control = control.replace('writeInterval   100;', 'writeInterval   1;')
control = control.replace('writeFormat     binary;', 'writeFormat     ascii;')
(case / 'system/controlDict').write_text(control)
print('Prepared installed motorBikeSteady tutorial with coarse refinement')
'''


NORMALIZE_FOR_RUNNER = r'''
from pathlib import Path
import gzip
import json
import subprocess

case = Path('/tmp/windcatcher-parallel-smoke')
subprocess.run(['foamDictionary', str(case / 'system/snappyHexMeshDict'), '-entry',
                'geometry/motorBike/file', '-set', '"../triSurface/motorBike.obj"'],
               check=True, capture_output=True)
# Expand with OpenFOAM's own parser, which writes to stdout. The caller writes
# that result only after a successful expansion; no in-place utility edits.
files = [case / 'system' / name for name in
         ('blockMeshDict', 'snappyHexMeshDict', 'fvSchemes', 'fvSolution', 'controlDict')]
files += [case / '0' / name for name in ('U', 'p', 'k', 'omega', 'nut')]
for path in files:
    before = path.read_bytes()
    result = subprocess.run(['foamDictionary', '-case', str(case), '-expand', str(path)],
                            check=True, capture_output=True, text=True)
    assert path.read_bytes() == before, 'Expansion unexpectedly changed the input'
    assert result.stdout.lstrip().startswith('/*') or 'FoamFile' in result.stdout
    path.write_text(result.stdout)

surface = case / 'constant/triSurface/motorBike.obj'
surface.parent.mkdir()
with gzip.open(case / 'constant/geometry/motorBike.obj.gz', 'rb') as source:
    surface.write_bytes(source.read())
(case / 'constant/geometry/motorBike.obj.gz').unlink()
(case / 'case_meta.json').write_text(json.dumps({
    'parallel': {'profile': 'custom', 'requested_subdomains': int(subprocess.check_output(
        ['foamDictionary', str(case / 'system/decomposeParDict'), '-entry',
         'numberOfSubdomains', '-value'], text=True).strip())},
    'openfoam_flavour': 'org',
    'stl_cleaning': {'open_edges': 0, 'nonmanifold_edges': 0},
}))
segment = case.parent / 'segment'
segment.mkdir()
(segment / 'segmentation.json').write_text(json.dumps({'need_human_confirm': False}))
print('Expanded static dictionaries with official foamDictionary; moved local surface to triSurface')
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='openfoam/openfoam11-paraview510:11')
    parser.add_argument('--processes', type=int, default=16)
    parser.add_argument('--reuse-processes', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--export-prepared-case', action='store_true',
                        help='Keep an unmeshed tutorial copy under .local-monitor for runner tests')
    args = parser.parse_args()
    if min(args.processes, args.reuse_processes) < 2:
        parser.error('Both process counts must be at least 2')
    if args.processes == args.reuse_processes:
        parser.error('Different counts are required to validate mesh repartitioning')

    name = f'windcatcher-parallel-smoke-{uuid.uuid4().hex[:12]}'
    timings: list[dict] = []
    case_path = '/tmp/windcatcher-parallel-smoke'

    def invoke(label: str, command: list[str], stdin: str | None = None) -> str:
        started = time.monotonic()
        result = subprocess.run(command, input=stdin, text=True, encoding='utf-8',
                                errors='replace', stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=args.timeout)
        elapsed = round(time.monotonic() - started, 3)
        timings.append({'step': label, 'seconds': elapsed, 'exit_code': result.returncode})
        print(json.dumps(timings[-1]), flush=True)
        if result.returncode:
            raise RuntimeError(f'{label} failed ({result.returncode}):\n{result.stdout[-14000:]}')
        return result.stdout

    def foam(label: str, command: str) -> str:
        shell = f'. /opt/openfoam11/etc/bashrc; cd {case_path}; {command}'
        return invoke(label, ['docker', 'exec', name, 'bash', '-lc', shell])

    def mpi(count: int, command: str) -> str:
        return f'mpirun --allow-run-as-root --use-hwthread-cpus -np {count} {command}'

    def decomposition(count: int) -> None:
        contents = ('FoamFile { format ascii; class dictionary; object decomposeParDict; }\n'
                    f'numberOfSubdomains {count};\nmethod scotch;\ndistributed no;\nroots ();\n')
        invoke(f'configure_{count}_processes',
               ['docker', 'exec', '-i', name, 'bash', '-c',
                f'cat > {case_path}/system/decomposeParDict'], contents)

    def mesh_hash() -> str:
        return foam('mesh_fingerprint',
                    'sha256sum constant/polyMesh/{points,faces,owner,neighbour,boundary}')

    def solve(count: int) -> None:
        foam(f'decompose_fields_{count}', 'decomposePar -force -time 0')
        verify = (
            'from pathlib import Path\nimport re\n'
            f'case = Path({case_path!r})\n'
            'parts = [p for p in case.iterdir() if re.fullmatch(r"processor[0-9]+", p.name)]\n'
            f'assert len(parts) == {count}, [p.name for p in parts]\n'
            'for part in parts:\n'
            '    assert (part / "0/U").is_file()\n'
            '    assert not (part / "3").exists(), "Old solver time survived force repartition"\n'
        )
        invoke(f'verify_fresh_decomposition_{count}',
               ['docker', 'exec', '-i', name, 'python3', '-'], verify)
        output = foam(f'solve_{count}', mpi(count, 'simpleFoam -parallel'))
        if not re.search(r'^Time = 3s?\s*$', output, re.M) or not re.search(r'^End\s*$', output, re.M):
            raise AssertionError(f'{count} process solve did not reach 3 iterations:\n{output[-12000:]}')
        foam(f'reconstruct_fields_{count}', 'reconstructPar -latestTime')
        foam(f'verify_fields_{count}', 'test -s 3/U && test -s 3/p')

    try:
        invoke('inspect_local_image', ['docker', 'image', 'inspect', args.image])
        # --pull=never prevents implicit network access. No host filesystem mounts.
        invoke('start_temporary_container',
               ['docker', 'run', '-d', '--pull=never', '--name', name,
                '--user', '0', '--entrypoint', 'sleep', args.image, 'infinity'])
        invoke('prepare_tutorial', ['docker', 'exec', '-i', name, 'python3', '-'], SETUP)
        decomposition(args.processes)
        if args.export_prepared_case:
            invoke('normalize_for_runner', ['docker', 'exec', '-i', name, 'bash', '-lc',
                   '. /opt/openfoam11/etc/bashrc; python3 -'], NORMALIZE_FOR_RUNNER)
            target = Path(__file__).resolve().parents[1] / '.local-monitor' / name / 'cfd'
            target.parent.mkdir(parents=True)
            invoke('export_prepared_case', ['docker', 'cp', f'{name}:{case_path}', str(target)])
            invoke('export_segmentation_gate', ['docker', 'cp', f'{name}:/tmp/segment', str(target.parent / 'segment')])
            print(json.dumps({'prepared_case': str(target.parent)}, ensure_ascii=False), flush=True)
        foam('mpi_preflight', mpi(max(args.processes, args.reuse_processes), 'true'))
        foam('blockMesh', 'blockMesh')
        before_hash = mesh_hash()
        # Decompose geometry only: uniform initial fields are initialized AFTER
        # snappy creates its final patches and reconstructs the serial mesh.
        foam('decompose_mesh', 'decomposePar -force -noFields')
        output = foam('snappyHexMesh_parallel', mpi(args.processes, 'snappyHexMesh -parallel -overwrite'))
        print(output[-2000:], flush=True)
        # OpenFOAM 11's reconstructParMesh only prints a deprecation notice.
        foam('reconstruct_mesh', 'reconstructPar -constant -noFields -time 0')
        after_hash = mesh_hash()
        if before_hash == after_hash:
            raise AssertionError('Serial root mesh was not replaced by the snappy mesh')
        quality = foam('checkMesh', 'checkMesh')
        print(quality[-2400:], flush=True)
        if 'Mesh OK.' not in quality:
            raise AssertionError('Small smoke mesh did not pass checkMesh')
        cells = re.search(r'^\s*cells:\s+(\d+)\s*$', quality, re.M)
        if not cells or int(cells.group(1)) <= 1280 or not re.search(r'^\s*motorBike\S*\s+\d+', quality, re.M):
            raise AssertionError('Snappy did not refine the block mesh or create a surface patch')
        solve(args.processes)
        decomposition(args.reuse_processes)
        archive = (
            'from pathlib import Path\nimport re\n'
            f'case = Path({case_path!r})\n'
            'archive = case / "previous-fields"\narchive.mkdir()\n'
            'for path in case.iterdir():\n'
            '    if path.is_dir() and re.fullmatch(r"[1-9][0-9]*", path.name):\n'
            '        path.rename(archive / path.name)\n'
        )
        invoke('archive_previous_fields', ['docker', 'exec', '-i', name, 'python3', '-'], archive)
        # Repartition the reconstructed serial mesh; do not remesh.
        solve(args.reuse_processes)
        if after_hash != mesh_hash():
            raise AssertionError('Repartitioning or short solving modified the serial cached mesh')
        print(json.dumps({'success': True, 'cells': int(cells.group(1)) if cells else None,
                          'mesh_processes': args.processes,
                          'repartitioned_processes': args.reuse_processes,
                          'steps': timings}, ensure_ascii=False), flush=True)
    finally:
        subprocess.run(['docker', 'rm', '-f', name], check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__':
    main()
