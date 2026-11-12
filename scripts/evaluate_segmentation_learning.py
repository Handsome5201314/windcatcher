"""Explicit local freeze/evaluate commands; never reads credentials or calls a model."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.segmentation_evaluation import freeze_cohort, evaluate_cohort
from pipeline.segmentation_learning import STRATEGIES, canonical
from pipeline.upgrade_gate import segmentation_readiness


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['freeze', 'evaluate'])
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--strategy', choices=STRATEGIES, default='identity_v1')
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding='utf-8-sig'))
    if args.operation == 'freeze':
        result = freeze_cohort(data)
    else:
        report = evaluate_cohort(data, args.strategy)
        result = {'evaluation': report, 'upgrade': segmentation_readiness(report)}
    # Caller explicitly selects a new local output; never overwrites a frozen set.
    with args.output.open('xb') as stream:
        stream.write(canonical(result))
    print(json.dumps({'status': 'written', 'operation': args.operation, 'auto_promotion': False}))


if __name__ == '__main__':
    main()
