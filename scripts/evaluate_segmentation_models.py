"""Sequential local candidate evaluation; no reference labels or promotion."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def formal_hashes(root):
    from pipeline.segmentation_predictor import file_hash
    return {p.relative_to(root).as_posix():file_hash(p)
        for name in ('volume','segment','stl','surface','mesh','cfd','geom_qc','scenarios')
        for p in sorted((root/name).rglob('*')) if p.is_file() and not p.is_symlink()}


def main():
    from pipeline import cases, segmentation_model_jobs as jobs, segmentation_predictor as predictor
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',action='append',required=True,dest='case_ids')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve()
    allowed=jobs.REPORT_ROOT.resolve()
    if not output.is_relative_to(allowed):
        parser.error('Report must stay in .local-models/evaluations for retention cleanup')
    if len(set(args.case_ids))!=len(args.case_ids):
        parser.error('Duplicate cases are not permitted')
    output.parent.mkdir(parents=True,exist_ok=True)
    report={'schema':1,'report_kind':'segmentation_model_development',
        'model_id':predictor.MODEL_ID,'cohort_kind':'development_engineering_only',
        'patient_grouping_verified':False,'reference_available':False,'accuracy_validated':False,
        'production_promoted':False,'created_at':jobs.now(),'cases':[]}
    def save(): jobs.write(output,report)
    save()
    for cid in args.case_ids:
        root=cases.case_dir(cid)
        before=formal_hashes(root)
        binding=predictor.input_binding(cid)
        run=jobs.start(cid,binding['mask_version'])
        row={'case_id':cid,'run_id':run['run_id'],'before_hashes':before,'status':'running'}
        report['cases'].append(row);save()
        print(json.dumps({'case_id':cid,'status':'running','run_id':run['run_id']}),flush=True)
        try:
            while True:
                status=jobs.get_record(cid,run['run_id'])
                if status['status'] not in ('queued','running'):break
                time.sleep(3)
        except KeyboardInterrupt:
            jobs.cancel(cid,run['run_id'])
            raise
        row.update(jobs.public(status))
        after=formal_hashes(root)
        row['formal_inputs_unchanged']=before==after
        row['after_hashes']=after
        save()
        print(json.dumps({key:row.get(key) for key in ('case_id','status','summary','elapsed_seconds',
            'peak_gpu_mib','formal_inputs_unchanged','message')},ensure_ascii=False),flush=True)
        if before!=after:raise RuntimeError('Formal files changed during evaluation; batch stopped')
    report['finished_at']=jobs.now();save()


if __name__=='__main__':main()
