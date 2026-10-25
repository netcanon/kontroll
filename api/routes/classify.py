"""Classify route — GET /classify/{collection}. Which execution backend(s) fit a collection."""
from fastapi import APIRouter, Depends, HTTPException

from api.deps import Catalog, get_catalog
from api.models import ClassifyResponse
from kontroll.service.classify import service_classify

router = APIRouter(tags=["inquiry"])


@router.get("/classify/{collection}", response_model=ClassifyResponse, summary="Classify a collection")
def classify(collection: str, cat: Catalog = Depends(get_catalog)) -> dict:
    """Return the execution backend(s) that fit `collection` (best first) + the top backend's
    requirements; 404 if the collection isn't installed locally, 409 `repository_data` if the best
    backend's `requires:` is not a list of param names."""
    result = service_classify(collection, backends=cat.backends)
    if result is None:
        raise HTTPException(status_code=404, detail="'%s' not installed locally" % collection)
    return result
