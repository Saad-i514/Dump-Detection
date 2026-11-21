"""Run with: python -m uvicorn api.main:app --host 127.0.0.1 --port 8000."""

from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from io import BytesIO
import os
from pathlib import Path
import secrets
import shutil
from threading import Lock
from tempfile import TemporaryDirectory, mkdtemp
from typing import Annotated

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel

from src.core.detector import Detection, RoadDamageDetector
from src.core.video import Track, VideoLimitError, analyse_video
from src.utils.config import resolve_model_path
from src.utils.constants import DAMAGE_CLASSES, DEFAULT_CONF_THRESHOLD
from api.jobs import VideoJobs

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_VIDEO_FRAMES = 1800
MAX_VIDEO_PIXELS = 1920 * 1080
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
    app.state.jobs = VideoJobs()
    yield
    app.state.jobs.close()
    del app.state.detector


app = FastAPI(title="Dump-Detection API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("RDD_CORS_ORIGINS", "").split(",") if origin.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)


@contextmanager
def inference_slot():
    if not app.state.inference_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Model busy; retry shortly", headers={"Retry-After": "5"})
    try:
        yield
    finally:
        app.state.inference_lock.release()


def authorize(key: Annotated[str | None, Security(api_key_header)] = None):
    expected = app.state.api_key
    if expected and (key is None or not secrets.compare_digest(key.encode(), expected.encode())):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class DetectionResponse(BaseModel):
    width: int
    height: int
    count: int
    detections: list[Detection]


class VideoResponse(BaseModel):
    frames_total: int
    frames_processed: int
    fps: float
    duration_s: float
    raw_detections: int
    unique_damages: int
    duplication_ratio: float
    tracks: list[Track]
    by_class: dict[str, int]
    by_severity: dict[str, int]
    peak_frame: int
    peak_count: int


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
        "max_video_bytes": MAX_VIDEO_BYTES,
        "max_video_frames": MAX_VIDEO_FRAMES,
        "max_video_frame_pixels": MAX_VIDEO_PIXELS,
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
    with inference_slot():
        detections = app.state.detector.detect(np.ascontiguousarray(rgb[:, :, ::-1]), conf=confidence)
    return DetectionResponse(
        width=rgb.shape[1], height=rgb.shape[0], count=len(detections), detections=detections,
    )


@app.post("/detect/video", response_model=VideoResponse, dependencies=[Security(authorize)])
def detect_video(
    file: Annotated[UploadFile, File(description="MP4, MOV, AVI, or MKV; maximum 50 MiB")],
    confidence: Annotated[float, Form(ge=0.0, le=1.0)] = DEFAULT_CONF_THRESHOLD,
    frame_stride: Annotated[int, Form(ge=1, le=10)] = 2,
):
    """Analyze a short clip synchronously; return tracked damages and severity totals."""
    if os.getenv("RDD_ASYNC_VIDEO_ONLY") == "1":
        raise HTTPException(status_code=409, detail="Use POST /detect/video/jobs and poll its status URL")
    with inference_slot(), TemporaryDirectory(prefix="rdd-video-") as directory:
        source = save_video(file, directory)
        return run_video(source, confidence, frame_stride)


def save_video(file: UploadFile, directory: str) -> Path:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".mp4", ".mov", ".avi", ".mkv"}:
        raise HTTPException(status_code=415, detail="Use MP4, MOV, AVI, or MKV")
    source = Path(directory) / ("upload" + suffix)
    size = 0
    with source.open("wb") as destination:
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_VIDEO_BYTES:
                raise HTTPException(status_code=413, detail="Video exceeds 50 MiB")
            destination.write(chunk)
    if not size:
        raise HTTPException(status_code=400, detail="Empty video")
    with source.open("rb") as stream:
        header = stream.read(16)
    # Reject playlists and image sequences before handing the file to FFmpeg.
    if not (header[4:8] in {b"ftyp", b"moov", b"mdat", b"wide"}
            or (header[:4] == b"RIFF" and header[8:12] == b"AVI ")
            or header[:4] == b"\x1aE\xdf\xa3"):
        raise HTTPException(status_code=400, detail="Invalid video container")
    return source


def run_video(source: Path, confidence: float, frame_stride: int) -> VideoResponse:
    try:
        summary = analyse_video(
            app.state.detector, source, conf=confidence, frame_stride=frame_stride,
            max_frames=MAX_VIDEO_FRAMES, max_frame_pixels=MAX_VIDEO_PIXELS,
        )
    except VideoLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid or unsupported video") from exc
    if not summary.frames_processed:
        raise HTTPException(status_code=400, detail="Video contains no decodable frames")
    return VideoResponse(**asdict(summary), duplication_ratio=summary.duplication_ratio)


@app.post("/detect/video/jobs", status_code=202, dependencies=[Security(authorize)])
def submit_video(
    file: Annotated[UploadFile, File(description="MP4, MOV, AVI, or MKV; maximum 50 MiB")],
    confidence: Annotated[float, Form(ge=0.0, le=1.0)] = DEFAULT_CONF_THRESHOLD,
    frame_stride: Annotated[int, Form(ge=1, le=10)] = 2,
):
    """Submit one video; poll status_url for completion. Results expire after one hour."""
    if not app.state.inference_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Model busy; retry shortly", headers={"Retry-After": "5"})
    directory = None
    try:
        directory = mkdtemp(prefix="rdd-video-")
        source = save_video(file, directory)
        job_id = app.state.jobs.submit(
            lambda: run_video(source, confidence, frame_stride), directory, app.state.inference_lock,
        )
    except Exception:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        app.state.inference_lock.release()
        raise
    return {"job_id": job_id, "status_url": f"/detect/video/jobs/{job_id}"}


@app.get("/detect/video/jobs/{job_id}", dependencies=[Security(authorize)])
def video_job(job_id: str):
    return app.state.jobs.get(job_id)
