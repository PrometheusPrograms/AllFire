"""FastAPI app entrypoint.

See docs/ARCHITECTURE.md §3/§6a for how remaining resource routers get
wired in here later. `import_data` is env-gated: it's only included when
`ENABLE_SPREADSHEET_IMPORT` is set, so the route is genuinely absent from
prod's route table rather than just hidden — see
docs/PRODUCTION_IMPORT_RUNBOOK.md for why.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import analytics, calculators, positions, trades
from app.config import settings

app = FastAPI(title="Trade Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=r"https://all-fire-.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(calculators.router)
app.include_router(trades.router)
app.include_router(analytics.router)
app.include_router(positions.router)

if settings.enable_spreadsheet_import:
    from app.api import import_data

    app.include_router(import_data.router)
