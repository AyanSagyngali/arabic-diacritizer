from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .providers import BudgetExceeded, build_router_from_env
from .store import Store
from .text import base_equal, diacritic_error_rate

app = FastAPI(title="Arabic Diacritization API", version="1.0.0")
router = build_router_from_env()
store = Store(os.environ.get("DIAC_DB", "diacritizer.db"))


class DiacritizeIn(BaseModel):
    text: str = Field(..., min_length=1)


class CorrectionIn(BaseModel):
    request_id: str
    corrected: str
    editor: str | None = None
    note: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "providers": [p.name + ":" + p.model for p in router.providers], "spent_today_usd": round(router.spent, 4)}


@app.post("/v1/diacritize")
def diacritize(body: DiacritizeIn):
    try:
        out, info, res, errors = router.run(body.text)
    except BudgetExceeded as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(413, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    assert base_equal(out, body.text)  # hard invariant: base text never changes
    rid = str(uuid.uuid4())
    store.log_request(rid, body.text, out, res, info)
    return {"request_id": rid, "text": out, "provider": res.provider, "model": res.model,
            "cost_usd": round(res.cost_usd, 6), "latency_ms": res.latency_ms, "guard": info, "fallbacks": errors}


@app.post("/v1/corrections", status_code=201)
def add_correction(body: CorrectionIn):
    req = store.get_request(body.request_id)
    if not req:
        raise HTTPException(404, "unknown request_id")
    if not base_equal(body.corrected, req["input"]):
        raise HTTPException(422, "correction must only change diacritics, not letters")
    der = diacritic_error_rate(body.corrected, req["output"])
    cid = store.add_correction(body.request_id, body.corrected, body.editor, body.note, der)
    return {"correction_id": cid, "der_of_model_output": round(der, 4)}


@app.get("/v1/corrections/export")
def export():
    return StreamingResponse(store.export_jsonl(), media_type="application/x-ndjson")


@app.get("/v1/stats")
def stats():
    return store.stats()
