import json

import numpy as np
import pytest

from test_segmentation_learning import sample_case
from pipeline import cases
from pipeline.volume import load_volume


def test_physical_geometry_is_preserved_and_mismatch_rejected(sample_case):
    from pipeline.segmentation_predictor import image_geometry, validate_prediction, CandidateError
    cid, mask = sample_case
    volume = load_volume(cid)
    g = image_geometry(volume)
    assert g['spacing'] == [.5, 1., 2.]
    assert g['direction'] == [1.,0.,0.,0.,1.,0.,0.,0.,1.]
    np.testing.assert_array_equal(validate_prediction(mask, volume, g), mask)
    with pytest.raises(CandidateError, match='几何'):
        validate_prediction(mask, volume, {**g, 'origin':[1.,0.,0.]})
    with pytest.raises(CandidateError, match='标签'):
        validate_prediction(mask.astype(float) * 2, volume, g)


def test_rotated_ct_geometry_round_trip(sample_case):
    from pipeline.segmentation_predictor import image_geometry, validate_prediction
    cid, mask = sample_case
    volume = load_volume(cid)
    volume.axis_dirs = ((0.,0.,1.),(-1.,0.,0.),(0.,1.,0.))
    volume.origin = (13.,-4.,72.)
    g = image_geometry(volume)
    assert g['direction'] == [0.,-1.,0.,1.,0.,0.,0.,0.,1.]
    np.testing.assert_array_equal(validate_prediction(mask, volume, g), mask)


def test_difference_is_not_accuracy_and_keeps_both_components():
    from pipeline.segmentation_predictor import comparison_summary
    old = np.zeros((8,10,12),dtype=np.uint8); old[2:5,3:6,4:6]=1
    new = old.copy(); new[2,3,4]=0; new[1,1,1]=1; new[6,8,10]=1
    before = new.copy()
    result = comparison_summary(old,new,(2.,1.,.5))
    assert result['added_voxels']==2 and result['removed_voxels']==1
    assert result['candidate_components']==3
    assert result['accuracy_status']=='reference_required'
    assert result['production_eligible'] is False
    assert set(result['jump_slices'])=={'axial','coronal','sagittal'}
    np.testing.assert_array_equal(new,before)


def test_invalid_and_empty_masks_fail():
    from pipeline.segmentation_predictor import validate_prediction, image_geometry, CandidateError
    from pipeline.volume import Volume
    v=Volume(np.zeros((3,4,5)),(1.,1.,1.),(0.,0.,0.),((0.,0.,1.),(0.,1.,0.),(1.,0.,0.)))
    for mask in [np.zeros(v.shape), np.full(v.shape,np.nan), np.ones((4,4,5))]:
        with pytest.raises(CandidateError):validate_prediction(mask,v,image_geometry(v))


def test_tissue_is_not_airway():
    from pipeline.segmentation_predictor import airway_labels, CandidateError
    np.testing.assert_array_equal(airway_labels(np.array([0,1,2,3,4])), [0,1,1,1,0])
    with pytest.raises(CandidateError):airway_labels(np.array([5]))


def test_case_binding_changes_when_official_mask_changes(sample_case):
    from pipeline.segmentation_predictor import input_binding
    cid, mask=sample_case
    first=input_binding(cid)
    mask[0,0,0]=1; np.save(cases.case_dir(cid)/'segment/mask.npy',mask)
    assert input_binding(cid)['source_sha256'] != first['source_sha256']
    assert 'source_dir' not in json.dumps(first)
