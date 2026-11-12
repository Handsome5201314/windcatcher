"""Isolated nnU-Net subprocess; input paths are issued by the local controller."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['nnUNet_compile'] = 'false'


def report_progress(run,stage):
    path=run/'progress.tmp'
    path.write_text(json.dumps({'stage':stage}),encoding='utf-8')
    for attempt in range(25):
        try:
            path.replace(run/'progress.json')
            return
        except PermissionError as exc:
            if os.name!='nt' or getattr(exc,'winerror',None) not in {5,32} or attempt==24:raise
            time.sleep(.01)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    request_path = Path(args.request).resolve()
    job = json.loads(request_path.read_text(encoding='utf-8'))
    run = request_path.parent
    def progress(stage):
        report_progress(run,stage)
        print(stage, flush=True)
    progress('loading_model')
    import numpy as np
    import SimpleITK as sitk
    import torch
    from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor
    from pipeline.segmentation_predictor import image_geometry, validate_prediction, airway_labels, model_manifest
    from pipeline.volume import Volume
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA is unavailable; CPU fallback is disabled')
    torch.set_num_threads(4)
    torch.cuda.reset_peak_memory_stats()
    source = Path(job['source_root'])
    meta = json.loads((source / 'volume/meta.json').read_text(encoding='utf-8'))
    volume = Volume(np.load(source / 'volume/hu.npy', mmap_mode='r', allow_pickle=False),
        tuple(meta['spacing_mm']), tuple(meta['origin_mm']), tuple(map(tuple, meta['axis_dirs'])))
    geometry = image_geometry(volume)
    class GpuPredictor(nnUNetPredictor):
        def _internal_predict_sliding_window_return_logits(self,data,slicers,do_on_device=True):
            if not do_on_device:
                raise RuntimeError('GPU prediction failed; CPU accumulation fallback is disabled')
            return super()._internal_predict_sliding_window_return_logits(data,slicers,do_on_device)
    predictor = GpuPredictor(tile_step_size=.5, use_gaussian=True, use_mirroring=True,
        perform_everything_on_device=True, device=torch.device('cuda'), verbose=False,
        verbose_preprocessing=False, allow_tqdm=False)
    # This archive names an unpublished custom LOSS trainer. Inference uses the
    # recorded architecture with strict weight loading, not a fabricated trainer.
    from nnunetv2.utilities.plans_handling.plans_handler import PlansManager
    from nnunetv2.utilities.label_handling.label_handling import determine_num_input_channels
    from nnunetv2.utilities.get_network_from_plans import get_network_from_plans
    folder = Path(job['model_folder'])
    _, verified_folder = model_manifest()
    if folder.resolve() != verified_folder:
        raise ValueError('Model path does not match the verified local model')
    dataset = json.loads((folder/'dataset.json').read_text())
    if dataset['labels'] != {'background':0,'rightnasal':1,'leftnasal':2,'pharynx':3,'tissue':4}:
        raise ValueError('Unexpected label definition')
    plans = PlansManager(json.loads((folder/'plans.json').read_text()))
    configuration = plans.get_configuration('3d_fullres')
    if configuration.network_arch_class_name != 'dynamic_network_architectures.architectures.unet.PlainConvUNet':
        raise ValueError('Unsupported network architecture')
    # The release also stores NumPy float32/float64 training metrics. Permit
    # only those data types; never enable arbitrary pickle constructors.
    with torch.serialization.safe_globals([np._core.multiarray.scalar,np.dtype,
            np.dtypes.Float64DType,np.dtypes.Float32DType]):
        checkpoint = torch.load(folder/'fold_0/checkpoint_final.pth',map_location='cpu',weights_only=True)
    if checkpoint['trainer_name'] != 'nnUNetCustomLossTrainer' or checkpoint['init_args']['configuration'] != '3d_fullres':
        raise ValueError('Unsupported checkpoint configuration')
    network = get_network_from_plans(configuration.network_arch_class_name,
        configuration.network_arch_init_kwargs, configuration.network_arch_init_kwargs_req_import,
        determine_num_input_channels(plans,configuration,dataset),
        plans.get_label_manager(dataset).num_segmentation_heads, allow_init=False, deep_supervision=False)
    network.load_state_dict(checkpoint['network_weights'], strict=True)
    predictor.manual_initialization(network,plans,configuration,[checkpoint['network_weights']],
        dataset,checkpoint['trainer_name'],checkpoint['inference_allowed_mirroring_axes'])
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='inference-', dir=run) as work:
        work = Path(work); incoming = work / 'input'; outgoing = work / 'output'
        incoming.mkdir(); outgoing.mkdir()
        image = sitk.GetImageFromArray(np.asarray(volume.array, dtype=np.float32))
        image.SetSpacing(geometry['spacing']); image.SetOrigin(geometry['origin']); image.SetDirection(geometry['direction'])
        sitk.WriteImage(image, str(incoming / 'volume_0000.nii.gz'))
        progress('inference')
        predictor.predict_from_files(str(incoming), str(outgoing), save_probabilities=False,
            overwrite=True, num_processes_preprocessing=1, num_processes_segmentation_export=1)
        progress('checking_geometry')
        output = sitk.ReadImage(str(outgoing / 'volume.nii.gz'))
        geometry_out = {'spacing': list(output.GetSpacing()), 'origin': list(output.GetOrigin()),
            'direction': list(output.GetDirection()), 'size': list(output.GetSize())}
        labels = sitk.GetArrayFromImage(output)
        raw = airway_labels(labels)
        np.save(run / 'labels.npy', labels.astype(np.uint8), allow_pickle=False)
        result = {'elapsed_seconds': round(time.monotonic() - started, 3),
            'peak_gpu_mib': round(torch.cuda.max_memory_allocated() / (1024 ** 2), 1),
            'device': torch.cuda.get_device_name(), 'geometry': geometry_out,
            'torch': torch.__version__, 'postprocessing': 'none',
            'airway_voxels': int(raw.sum()), 'label_values': np.unique(labels).tolist(),
            'worker_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'inference_settings':{'tile_step_size':.5,'mirroring':True,'gaussian':True,
                'folds':[0],'deterministic_algorithms':torch.are_deterministic_algorithms_enabled(),
                'cpu_accumulation_fallback':False,'checkpoint_loading':'weights_only_allowlisted_numpy_scalars'}}
        (run / 'worker-result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        raw = validate_prediction(raw, volume, geometry_out)
        np.save(run / 'candidate.npy', raw, allow_pickle=False)
    progress('complete')


if __name__ == '__main__':
    try:
        from filelock import FileLock
        if '--request' in sys.argv:
            request_folder=Path(sys.argv[sys.argv.index('--request')+1]).parent
            report_progress(request_folder,'waiting_gpu')
        with FileLock(str(ROOT/'.local-models/inference.lock'), timeout=1800):
            main()
    except Exception as exc:
        if '--request' in sys.argv:
            folder=Path(sys.argv[sys.argv.index('--request')+1]).parent
            message = ('GPU 显存不足，候选未完成' if 'out of memory' in str(exc).lower()
                       else f'模型推理失败（{type(exc).__name__}），详见本机 worker.log')
            (folder/'worker-error.json').write_text(json.dumps({'message':message}),encoding='utf-8')
        raise
