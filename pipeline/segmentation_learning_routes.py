"""Case-scoped adapters; the public gateway applies explicit case grants."""
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from . import segmentation_learning as learning
from .endplane_history import CURRENT_ACTOR_ID

SAMPLE_PATTERN = r'^seg-[0-9a-f]{32}$'


class CandidateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sample_id: str = Field(pattern=SAMPLE_PATTERN)
    strategy_id: Literal['identity_v1', 'remove_tiny_islands_v1', 'remove_small_islands_v1']


class ReferenceBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sample_id: str = Field(pattern=SAMPLE_PATTERN)
    mask_version: int = Field(ge=0, strict=True)
    disposition: Literal['approved', 'rejected']
    reason_code: Literal['external_residual', 'missing_airway', 'sinus_residual', 'broken_connection', 'boundary_error', 'mixed']
    reference_checked: bool = Field(strict=True)
    use_authorized: bool = Field(strict=True)
    subject_group: str = Field(pattern=r'^subject-[A-Za-z0-9_-]{3,64}$')


def create_router(require_case) -> APIRouter:
    router = APIRouter()
    prefix = '/api/cases/{case_id}/segment/learning'

    async def invoke(operation, *args, **kwargs):
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except learning.LearningError as exc:
            raise HTTPException(409, detail={'reason': str(exc)}) from exc

    @router.get(prefix)
    async def inspect(case_id: str):
        require_case(case_id)
        samples = await invoke(learning.list_changes, case_id)
        return {'case_id': case_id, 'scope': 'main_airway', 'samples': samples,
            'latest_comparison': await invoke(learning.latest_comparison, case_id),
            'strategies': list(learning.STRATEGIES), 'auto_promotion': False,
            'note': '修正记录不会自动成为参考；候选仅在隔离副本运行。'}

    @router.post(prefix + '/candidate')
    async def candidate(case_id: str, body: CandidateBody):
        require_case(case_id)
        return await invoke(learning.run_candidate, case_id, body.sample_id, body.strategy_id)

    @router.post(prefix + '/reference')
    async def reference(case_id: str, body: ReferenceBody, request: Request):
        require_case(case_id)
        principal = getattr(request.state, 'principal', None)
        if ('x-windcatcher-mcp-token' in request.headers
                or (principal and principal.get('kind') != 'human')):
            raise HTTPException(403, detail={'reason': '参考审核只能由操作者在工作台完成'})
        return await invoke(learning.review_reference, case_id, body.sample_id, body.mask_version,
            reviewer_id=CURRENT_ACTOR_ID.get() or 'local-operator',
            **body.model_dump(exclude={'sample_id', 'mask_version'}))

    return router
