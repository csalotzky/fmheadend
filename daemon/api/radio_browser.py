"""Read-only directory endpoints; no control executor or Source mutations."""
from uuid import UUID
from fastapi import APIRouter, Query, Request, Response
from ..errors import DomainError
from ..models.configuration import ErrorResponse
from ..radio_browser import Station, Stations, Option

router = APIRouter(prefix='/api/v1/radio-browser', tags=['radio-browser'], responses={
    code: {'model': ErrorResponse} for code in (404, 422, 500, 503)
})


def client(request, response):
    response.headers['Cache-Control'] = 'no-store'
    return request.app.state.services.radio_browser


@router.get('/stations/search', response_model=Stations)
async def search(request: Request, response: Response, name: str = Query('', max_length=200),
                 countrycode: str | None = Query(None, pattern=r'^[A-Za-z]{2}$'),
                 tag: str | None = Query(None, max_length=100), offset: int = Query(0, ge=0, le=100000),
                 limit: int = Query(25, ge=1, le=100), hidebroken: bool = True):
    return await client(request, response).search(name=name, countrycode=countrycode, tag=tag,
                                                 offset=offset, limit=limit, hidebroken=hidebroken)


@router.get('/stations/{station_uuid}', response_model=Station)
async def lookup(station_uuid: UUID, request: Request, response: Response):
    value = await client(request, response).lookup(station_uuid)
    if value is None:
        raise DomainError(404, 'RADIO_BROWSER_NOT_FOUND', 'Radio Browser station was not found')
    return value


@router.get('/countrycodes', response_model=list[Option])
async def countries(request: Request, response: Response, offset: int = Query(0, ge=0, le=100000),
                    limit: int = Query(250, ge=1, le=250)):
    return await client(request, response).countries(offset=offset, limit=limit)


@router.get('/tags', response_model=list[Option])
async def tags(request: Request, response: Response, name: str = Query('', max_length=100),
               offset: int = Query(0, ge=0, le=100000), limit: int = Query(25, ge=1, le=100)):
    return await client(request, response).tags(name=name, offset=offset, limit=limit)
