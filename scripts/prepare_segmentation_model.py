"""Fetch one pinned public model; never read or upload case data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1] / '.local-models' / 'upper-airway-fink-v1'
URL = 'https://github.com/alejandro-matos/SlicerUpperAirwaySegmentator/releases/download/v1.1.0/Dataset001_Airways_Fink.zip'
SHA256 = 'a58b20100db3a99a6e7a630c9655aa4ee9ffaddbda77a8c0649e311f0092a8f1'
SIZE = 232485348


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    archive = ROOT / 'model.zip'
    if not archive.exists():
        partial = ROOT / 'download.part'
        request = urllib.request.Request(URL, headers={'User-Agent': 'Windcatcher-model-setup'})
        with urllib.request.urlopen(request, timeout=120) as source, partial.open('wb') as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
        if partial.stat().st_size != SIZE or file_hash(partial) != SHA256:
            raise ValueError('Model download failed size/SHA-256 verification')
        partial.replace(archive)
    if archive.stat().st_size != SIZE or file_hash(archive) != SHA256:
        raise ValueError('Existing model archive failed integrity verification')
    destination = ROOT / 'weights'
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        if sum(i.file_size for i in z.infolist()) > 2_000_000_000:
            raise ValueError('Unexpected uncompressed model size')
        for info in z.infolist():
            p = PurePosixPath(info.filename.replace('\\', '/'))
            if p.is_absolute() or '..' in p.parts or ':' in str(p):
                raise ValueError('Unsafe model archive path')
            target = destination.joinpath(*p.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Model archive path escaped destination')
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as source, target.open('wb') as out:
                    shutil.copyfileobj(source, out)
    datasets = list(destination.rglob('dataset.json'))
    if len(datasets) != 1:
        raise ValueError('Expected exactly one model dataset.json')
    model_folder = datasets[0].parent
    required = [model_folder / 'dataset.json', model_folder / 'plans.json', *model_folder.glob('fold_*/checkpoint_final.pth')]
    if len(required) < 3 or not all(p.is_file() for p in required):
        raise ValueError('Incomplete model package')
    manifest = {'schema': 1, 'model_id': 'upper_airway_fink_v1', 'source_url': URL,
        'release': 'v1.1.0', 'archive_sha256': SHA256,
        'source_commit': '6313dcbcf53db2e0d9331d7c7850a235d93b88c0',
        'model_folder': str(model_folder.relative_to(ROOT)),
        'files': {str(p.relative_to(ROOT)): file_hash(p) for p in required},
        'dataset': json.loads(datasets[0].read_text(encoding='utf-8')),
        'use': 'local_evaluation_candidate_only', 'production_eligible': False}
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
