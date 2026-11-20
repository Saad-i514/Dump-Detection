"""Exercise the HTTP contract against the installed, real model."""

from io import BytesIO
from pathlib import Path

import numpy as np
import cv2
from PIL import Image
import pytest
from fastapi.testclient import TestClient

from api import main
from src.utils.config import resolve_model_path


@pytest.fixture(scope="module")
def client():
    if not resolve_model_path()[1]:
        pytest.skip("API integration tests require trained weights in models/")
    with TestClient(main.app) as connection:
        yield connection


@pytest.fixture(autouse=True)
def local_auth(client):
    previous = main.app.state.api_key
    main.app.state.api_key = ""
    yield
    main.app.state.api_key = previous


def test_health_model_and_openapi(client):
    assert client.get("/health").json() == {"status": "ok", "model_loaded": True}
    info = client.get("/model").json()
    assert [item["code"] for item in info["classes"]] == ["D00", "D10", "D20", "D40"]
    assert "/detect" in client.get("/openapi.json").json()["paths"]
    assert client.get("/docs").status_code == 200


def test_real_image_matches_detector(client):
    sample = next(Path("samples").glob("*.jpg"))
    with Image.open(sample) as image:
        rgb = np.asarray(image.convert("RGB"))
    expected = main.app.state.detector.detect(np.ascontiguousarray(rgb[:, :, ::-1]), conf=0.25)
    response = client.post("/detect", files={"file": (sample.name, sample.read_bytes(), "image/jpeg")})
    assert response.status_code == 200, response.text
    data = response.json()
    assert (data["width"], data["height"]) == (rgb.shape[1], rgb.shape[0])
    assert data["count"] == len(data["detections"]) == len(expected)
    for actual, detection in zip(data["detections"], expected):
        assert actual["class_id"] == detection.class_id
        assert actual["bbox"] == pytest.approx(detection.bbox)
        assert actual["severity_score"] == pytest.approx(detection.severity_score)


@pytest.mark.parametrize("body,status", [(b"", 400), (b"not an image", 400)])
def test_bad_images(client, body, status):
    assert client.post("/detect", files={"file": ("bad.jpg", body)}).status_code == status


def test_upload_limit(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 8)
    assert client.post("/detect", files={"file": ("big.jpg", b"x" * 9)}).status_code == 413


def test_pixel_limit(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_IMAGE_PIXELS", 3)
    data = BytesIO()
    Image.new("RGB", (2, 2)).save(data, format="PNG")
    assert client.post("/detect", files={"file": ("big.png", data.getvalue())}).status_code == 413


def test_unsupported_format(client):
    data = BytesIO()
    Image.new("RGB", (2, 2)).save(data, format="GIF")
    assert client.post("/detect", files={"file": ("image.gif", data.getvalue())}).status_code == 415


@pytest.mark.parametrize("confidence", ["-0.1", "1.1", "nan", "garbage"])
def test_invalid_confidence(client, confidence):
    assert client.post("/detect", files={"file": ("x.jpg", b"x")},
                       data={"confidence": confidence}).status_code == 422


def test_missing_file(client):
    assert client.post("/detect").status_code == 422


def test_api_key(client):
    import secrets

    key = secrets.token_urlsafe(24)
    main.app.state.api_key = key
    assert client.get("/health").status_code == 200
    assert client.get("/model").status_code == 401
    assert client.get("/model", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/model", headers={"X-API-Key": key}).status_code == 200
    assert client.post("/detect", files={"file": ("x.jpg", b"x")}).status_code == 401
    assert client.post("/detect/video", files={"file": ("x.mp4", b"x")}).status_code == 401


def test_no_detections_is_valid(client):
    data = BytesIO()
    Image.new("RGB", (64, 64)).save(data, format="PNG")
    response = client.post("/detect", files={"file": ("blank.png", data.getvalue())},
                           data={"confidence": "1.0"})
    assert response.status_code == 200
    assert response.json()["detections"] == []


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.avi"
    frame = cv2.imread(str(next(Path("samples").glob("*.jpg"))))
    frame = cv2.resize(frame, (640, 480))
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (640, 480))
    assert writer.isOpened()
    try:
        for _ in range(6):
            writer.write(frame)
    finally:
        writer.release()
    return path


def test_video_real_inference_and_tracking(client, video):
    response = client.post("/detect/video", files={"file": (video.name, video.read_bytes())},
                           data={"frame_stride": "2"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["frames_total"] == 6
    assert result["frames_processed"] == 3
    assert result["duration_s"] == pytest.approx(0.6)
    assert result["unique_damages"] == len(result["tracks"])
    assert result["raw_detections"] == sum(t["hits"] for t in result["tracks"])
    assert result["unique_damages"] == sum(result["by_class"].values())
    assert "output_path" not in result


@pytest.mark.parametrize("filename,payload,status", [
    ("clip.txt", b"x", 415), ("clip.mp4", b"", 400),
    ("clip.mp4", b"#EXTM3U\nhttps://example.com", 400),
    ("clip.mp4", b"\x00\x00\x00\x10ftyp" + b"broken", 400),
])
def test_invalid_video(client, filename, payload, status):
    assert client.post("/detect/video", files={"file": (filename, payload)}).status_code == status


@pytest.mark.parametrize("stride", ["0", "11", "1.5"])
def test_invalid_video_stride(client, stride):
    assert client.post("/detect/video", files={"file": ("clip.mp4", b"x")},
                       data={"frame_stride": stride}).status_code == 422


@pytest.mark.parametrize("limit,value", [
    ("MAX_VIDEO_BYTES", 8), ("MAX_VIDEO_FRAMES", 2), ("MAX_VIDEO_PIXELS", 100),
])
def test_video_limits_and_temp_cleanup(client, video, monkeypatch, tmp_path, limit, value):
    import tempfile

    monkeypatch.setattr(main, limit, value)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    response = client.post("/detect/video", files={"file": (video.name, video.read_bytes())})
    assert response.status_code == 413, response.text
    assert not list(tmp_path.glob("rdd-video-*"))
