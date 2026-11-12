"""Authenticated case-scoped, candidate-only model operations."""
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from . import segmentation_model_jobs as jobs


class ModelBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    model_id: Literal['upper_airway_fink_v1'] = 'upper_airway_fink_v1'
    mask_version: int = Field(ge=0,strict=True)


def create_router(require_case):
    router=APIRouter()
    prefix='/api/cases/{case_id}/segment/models'
    async def call(operation,*args):
        try:
            return await asyncio.to_thread(operation,*args)
        except jobs.CandidateError as exc:
            raise HTTPException(409,detail={'reason':str(exc)}) from exc
    @router.get(prefix)
    async def inspect(case_id:str):
        require_case(case_id)
        return await call(jobs.inspect,case_id)
    @router.post(prefix,status_code=202)
    async def start(case_id:str,body:ModelBody):
        require_case(case_id)
        return await call(jobs.start,case_id,body.mask_version,body.model_id)
    @router.get(prefix+'/{run_id}')
    async def status(case_id:str,run_id:str):
        require_case(case_id)
        return jobs.public(await call(jobs.get_record,case_id,run_id))
    @router.post(prefix+'/{run_id}/cancel')
    async def cancel(case_id:str,run_id:str):
        require_case(case_id)
        return await call(jobs.cancel,case_id,run_id)
    @router.post(prefix+'/{run_id}/draft')
    async def draft(case_id:str,run_id:str):
        require_case(case_id)
        return await call(jobs.adopt_draft,case_id,run_id)
    @router.get(prefix+'/{run_id}/slice/{view}')
    async def slice_image(case_id:str,run_id:str,view:Literal['axial','coronal','sagittal'],
            index:int=0,mode:Literal['baseline','candidate','difference']='difference',wl:float=-500,ww:float=2000):
        require_case(case_id)
        try:
            data=await call(jobs.render,case_id,run_id,view,index,mode,wl,ww)
        except ValueError as exc:
            raise HTTPException(400,detail={'reason':str(exc)}) from exc
        return Response(data,media_type='image/png',headers={'Cache-Control':'no-store'})
    return router
