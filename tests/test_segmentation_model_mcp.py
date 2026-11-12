import asyncio
import pytest
pytest.importorskip('mcp')
import windcatcher_mcp as adapter


def test_candidate_tools_are_registered_without_pixel_or_approval_tools(monkeypatch):
    names=set(adapter.mcp._tool_manager._tools)
    required={'inspect_segmentation_models','start_segmentation_model','segmentation_model_status','cancel_segmentation_model'}
    assert required<=names
    assert not {'approve_segmentation_model','segmentation_model_slice'} & names
    calls=[]
    async def request(method,path,**kwargs):
        calls.append((method,path,kwargs));return {'committed':False}
    monkeypatch.setattr(adapter,'_request',request)
    cid='case-20261106-120000-abcd';rid='model-'+'a'*32
    asyncio.run(adapter.inspect_segmentation_models(cid))
    asyncio.run(adapter.start_segmentation_model(cid,2))
    asyncio.run(adapter.segmentation_model_status(cid,rid))
    asyncio.run(adapter.cancel_segmentation_model(cid,rid))
    assert [r[0] for r in calls]==['GET','POST','GET','POST']
    assert calls[1][2]['body']=={'mask_version':2,'model_id':'upper_airway_fink_v1'}
    assert all('/segment/models' in r[1] for r in calls)
    assert calls[-1][1].endswith('/'+rid+'/cancel')
