"""Trade event (OPEN/ROLL/ADJUST/CLOSE/EXPIRE/ASSIGN) routes — not yet implemented."""

from fastapi import APIRouter

router = APIRouter(prefix="/events", tags=["events"])
