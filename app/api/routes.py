from fastapi import APIRouter

router = APIRouter()


@router.get("/status")
def get_status() -> dict[str, str]:
    """Return a basic API status payload for the mobile client."""
    return {"status": "ready"}
