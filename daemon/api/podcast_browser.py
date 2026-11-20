"""Read-only Apple Podcasts directory endpoint."""
from fastapi import APIRouter, Query, Request, Response

from ..models.configuration import ErrorResponse
from ..podcast_browser import MAX_QUERY_LENGTH, Podcasts

router = APIRouter(prefix='/api/v1/podcast-browser', tags=['podcast-browser'], responses={
    code: {'model': ErrorResponse} for code in (422, 429, 503)
})


@router.get('/search', response_model=Podcasts)
async def search(request: Request, response: Response, name: str = Query(..., max_length=MAX_QUERY_LENGTH)):
    response.headers['Cache-Control'] = 'no-store'
    return await request.app.state.services.podcast_browser.search(name)
