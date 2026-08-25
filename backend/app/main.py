"""FastAPI app entrypoint.

No routes are registered yet — see docs/ARCHITECTURE.md §3/§6a for how
resource routers and env-gated routers get wired in here later.
"""

from fastapi import FastAPI

app = FastAPI(title="Trade Tracker API")
