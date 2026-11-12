"""Real GPU/checkpoint smoke on a synthetic volume; never an accuracy test."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import numpy as np
    from pipeline import segmentation_predictor as predictor
    from pipeline.volume import Volume
    manifest, folder = predictor.model_manifest()
    run = predictor.MODEL_ROOT / ('synthetic-check-' + time.strftime('%Y%m%d-%H%M%S'))
    source = run / 'synthetic-source'
    (source / 'volume').mkdir(parents=True, exist_ok=False)
    z, y, x = np.indices((64, 96, 96))
    hu = np.full(z.shape, -1000, dtype=np.float32)
    hu[((x-48)/38)**2 + ((y-48)/38)**2 < 1] = 40
    hu[((x-42)**2 + (y-43)**2 < 25) | ((x-54)**2 + (y-43)**2 < 25)] = -850
    vol = Volume(hu, (.7,.6,.5), (12.,-31.,5.), ((0.,0.,1.),(-1.,0.,0.),(0.,1.,0.)))
    np.save(source/'volume/hu.npy', hu, allow_pickle=False)
    (source/'volume/meta.json').write_text(json.dumps({'spacing_mm':vol.spacing,
        'origin_mm':vol.origin,'axis_dirs':vol.axis_dirs}), encoding='utf-8')
    (run/'request.json').write_text(json.dumps({'source_root':str(source),
        'model_folder':str(folder)}), encoding='utf-8')
    with (run/'worker.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([str(predictor.PYTHON), '-u', str(ROOT/'scripts/segmentation_model_worker.py'),
            '--request', str(run/'request.json')], stdout=log, stderr=log, timeout=600,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if not (run/'worker-result.json').is_file():
        raise RuntimeError('Real model smoke failed; inspect '+str(run/'worker.log'))
    details = json.loads((run/'worker-result.json').read_text(encoding='utf-8'))
    expected = predictor.image_geometry(vol)
    assert all(np.allclose(details['geometry'][key],value,atol=1e-4,rtol=0) for key,value in expected.items())
    labels = np.load(run/'labels.npy',allow_pickle=False)
    assert labels.shape == vol.shape
    candidate = predictor.airway_labels(labels)
    if not candidate.any():
        assert result.returncode != 0 and not (run/'candidate.npy').exists()
        outcome = 'synthetic_empty_rejected'
    else:
        assert result.returncode == 0
        predictor.validate_prediction(np.load(run/'candidate.npy',allow_pickle=False),vol,details['geometry'])
        outcome = 'synthetic_candidate_generated'
    marker = {'schema':1,'model_id':predictor.MODEL_ID,'archive_sha256':manifest['archive_sha256'],
        'verified_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'synthetic_only':True,
        'accuracy_validated':False,'outcome':outcome,'evidence_directory':run.name, **details}
    (predictor.MODEL_ROOT/'runtime-verified.json').write_text(json.dumps(marker,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(marker,ensure_ascii=False))


if __name__ == '__main__':
    main()
