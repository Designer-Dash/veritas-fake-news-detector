"""FastAPI app exposing POST /analyze and GET /health for the extension."""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .pipeline import analyze

app = FastAPI(title="Fake News Detector API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # extension content scripts can't set Origin reliably
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    url: str = Field(..., description="Page URL to analyze")
    region: str = Field("in", description="Region key: in | us | uk | global")
    html: Optional[str] = Field(None, description="Optional DOM HTML captured by the extension")
    headline: Optional[str] = Field(None, description="Optional headline override")
    include_cross_check: bool = Field(True, description="Query trusted outlets for corroboration")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": app.version}


@app.post("/analyze")
def analyze_endpoint(req: AnalyzeRequest) -> dict:
    result = analyze(
        url=req.url,
        region=req.region,
        html=req.html,
        headline_override=req.headline,
        include_cross_check=req.include_cross_check,
    )
    return result.to_dict()
