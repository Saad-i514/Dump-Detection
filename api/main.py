"""Run with: python -m uvicorn api.main:app --host 127.0.0.1 --port 8000."""

from contextlib import asynccontextmanager
from dataclasses import asdict
from io import BytesIO
import os
from pathlib import Path
import secrets
from threading import Lock
from typing import Annotated

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.security import APIKeyHeader
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel

from src.core.detector import Detection, RoadDamageDetector
from src.utils.config import resolve_model_path
from src.utils.constants import DAMAGE_CLASSES, DEFAULT_CONF_THRESHOLD

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    path, trained = resolve_model_path()
    if not trained:
        raise RuntimeError("Place trained weights in models/road_damage_best.pt before starting the API.")
    detector = RoadDamageDetector(path)
    labels = detector._model.names
    names = [str(labels.get(i, "")).lower().replace(" ", "") for i in range(len(labels))]
    accepted = (
        {"d00", "longitudinalcrack"}, {"d10", "transversecrack"},
        {"d20", "alligatorcrack"}, {"d40", "pothole", "potholes"},
    )
    if len(names) != 4 or any(name not in allowed for name, allowed in zip(names, accepted)):
        raise RuntimeError("Model class order must be D00, D10, D20, D40.")
    app.state.detector = detector
    app.state.inference_lock = Lock()
    app.state.api_key = os.getenv("RDD_API_KEY", "")
    yield
    del app.state.detector


app = FastAPI(title="Dump-Detection API", version="1.0.0", lifespan=lifespan)


def authorize(key: Annotated[str | None, Security(api_key_header)] = None):
    expected = app.state.api_key
    if expected and (key is None or not secrets.compare_digest(key.encode(), expected.encode())):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class DetectionResponse(BaseModel):
    width: int
    height: int
    count: int
    detections: list[Detection]


@app.get("/health")
def health():
    """Readiness probe; available without an API key."""
    return {"status": "ok", "model_loaded": hasattr(app.state, "detector")}


@app.get("/model", dependencies=[Security(authorize)])
def model_info():
    """Active model and the class taxonomy used in detection responses."""
    detector = app.state.detector
    return {
        "model": Path(detector.model_path).name,
        "backend": detector.backend,
        "classes": [asdict(item) for item in DAMAGE_CLASSES],
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_image_pixels": MAX_IMAGE_PIXELS,
    }


@app.post("/detect", response_model=DetectionResponse, dependencies=[Security(authorize)])
def detect(
    file: Annotated[UploadFile, File(description="JPEG, PNG, or WebP image; maximum 10 MiB")],
    confidence: Annotated[float, Form(ge=0.0, le=1.0)] = DEFAULT_CONF_THRESHOLD,
):
    """Return bounding boxes in pixels and severity scores. Images are not persisted."""
    payload = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds 10 MiB")
    if not payload:
        raise HTTPException(status_code=400, detail="Empty image")
    try:
        with Image.open(BytesIO(payload)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(status_code=415, detail="Use a JPEG, PNG, or WebP image")
            if source.width * source.height > MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="Image exceeds 20 megapixels")
            rgb = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))
    except Image.DecompressionBombError as exc:
        raise HTTPException(status_code=413, detail="Image exceeds pixel limit") from exc
    # Ultralytics' PIL wrapper can raise ImportError when corrupt input triggers its HEIF fallback.
    except (UnidentifiedImageError, OSError, ValueError, ImportError) as exc:
        raise HTTPException(status_code=400, detail="Invalid or corrupt image") from exc

    # Ultralytics reuses mutable predictor state across calls.
    with app.state.inference_lock:
        detections = app.state.detector.detect(np.ascontiguousarray(rgb[:, :, ::-1]), conf=confidence)
    return DetectionResponse(
        width=rgb.shape[1], height=rgb.shape[0], count=len(detections), detections=detections,
    )
