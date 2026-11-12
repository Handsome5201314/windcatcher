"""Real gateway authorization using synthetic candidates and isolated identities."""
import json
import pytest
from test_segmentation_learning_gateway import gateway, sample_case
from test_segmentation_model_jobs import model_runtime, finish
from pipeline import segmentation_model_jobs as jobs


def test_model_mcp_requires_edit_grant_and_never_serves_pixels(gateway,model_runtime):
    g=gateway;cid=g['cid'];path=f'/api/cases/{cid}/segment/models'
    headers={'X-Windcatcher-MCP-Token':'T'*43}
    local=g['local'];viewer=g['viewer']
    assert local.get(path,headers=headers).status_code in {403,404}
    assert viewer.get(path).status_code in {403,404}
    for principal in (g['service_id'],g['viewer_id']):
        g['identity'].grant_case(g['owner_id'],cid,principal,'viewer')
    assert local.get(path,headers=headers).status_code==200
    assert local.post(path,json={'mask_version':2},headers=headers).status_code in {403,404}
    assert viewer.post(path,json={'mask_version':2}).status_code in {403,404}
    g['identity'].grant_case(g['owner_id'],cid,g['service_id'],'editor')
    response=local.post(path,json={'mask_version':2},headers=headers)
    assert response.status_code==202,response.text
    row=finish(cid,response.json());assert row['status']=='succeeded'
    rid=row['run_id']
    response=local.get(path+'/'+rid,headers=headers)
    assert response.status_code==200
    assert not {'binding','actor_id','source_root','candidate_sha256'} & response.json().keys()
    assert response.json()['production_eligible'] is False
    assert local.get(path+'/'+rid+'/slice/axial',headers=headers).status_code in {401,403}
    assert viewer.get(path+'/'+rid+'/slice/axial').status_code==200
    assert local.post(path+'/'+rid+'/approve',json={},headers=headers).status_code in {401,403}
    assert local.get(path,headers={**headers,'X-Forwarded-For':'127.0.0.1'}).status_code in {401,403}
    assert g['owner'].get(f'/api/cases/{cid}/artifact',params={'rel':f'learning/segmentation_models/{rid}/labels.npy'}).status_code==403


def test_another_case_cannot_read_candidate_record(gateway,model_runtime):
    from pipeline import cases
    g=gateway;cid=g['cid'];run=finish(cid,jobs.start(cid,2))
    other=cases.create_case('synthetic','<synthetic-other>',synthetic=True)
    response=g['owner'].get(f'/api/cases/{other}/segment/models/'+run['run_id'])
    assert response.status_code in {403,404,409}
    assert 'summary' not in response.text
