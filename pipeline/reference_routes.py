"""Public allowlisted references and authenticated operator publication routes."""
from contextlib import ExitStack
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import reference_cases


class ReferenceBody(BaseModel):
    title: str = Field(min_length=2, max_length=80)
    description: str = Field(default='', max_length=600)
    tags: list[str] = Field(default_factory=list, max_length=8)
    authorization_confirmed: bool = False
    labels_confirmed: bool = False
    reviewer: str = Field(min_length=2, max_length=80)
    review_note: str = Field(min_length=10, max_length=1000)
    kind: Literal['anatomy', 'flow', 'flow_test'] = 'anatomy'
    test_use_confirmed: bool = False
    action: Literal['draft', 'publish'] = 'draft'


def install(app, server, access, root):
    @app.get('/references')
    async def reference_page():
        return HTMLResponse((root / 'deploy/reference-view.html').read_text(encoding='utf-8'))

    @app.get('/api/references')
    async def reference_catalog(tag: str | None = None):
        return await run_in_threadpool(reference_cases.catalog, tag)

    @app.get('/api/references/{reference_id}/model')
    async def reference_model(reference_id: str, version: str | None = None):
        return Response(await run_in_threadpool(reference_cases.read_model, reference_id, version), media_type='application/json')

    @app.get('/api/reference-admin/cases')
    async def reference_candidates(request: Request):
        result = await run_in_threadpool(reference_cases.candidates)
        if access.multiuser:
            actor_id = request.state.principal['id']
            allowed = set(access.identity.allowed_case_ids(
                actor_id, [item['case_id'] for item in result['items']]))
            result['items'] = [item for item in result['items'] if item['case_id'] in allowed]
        return result

    @app.post('/api/reference-admin/cases/{case_id}')
    async def reference_save(case_id: str, body: ReferenceBody, request: Request):
        if body.kind == 'flow_test' and access.deployment != 'public':
            raise HTTPException(404, detail={'reason': '测试气流仅限本机公网入口'})
        with ExitStack() as reservations:
            queue = getattr(server.app.state, 'batch_queue', None)
            if queue:
                reservations.enter_context(queue.mutation(case_id))
            server.cfd_postprocess_jobs.reserve_mutation(case_id)
            reservations.callback(server.cfd_postprocess_jobs.release_mutation, case_id)
            server.scenario_jobs.reserve_mutation(case_id)
            reservations.callback(server.scenario_jobs.release_mutation, case_id)
            actor = (access.actor_name(request) if access.multiuser else
                     access.username + '/' + body.reviewer.strip())
            return await run_in_threadpool(reference_cases.save, case_id, body.model_dump(), actor)

    @app.post('/api/reference-admin/{reference_id}/revoke')
    async def reference_revoke(reference_id: str, request: Request):
        await run_in_threadpool(reference_cases.revoke, reference_id, access.actor_name(request))
        return {'status': 'withdrawn'}
