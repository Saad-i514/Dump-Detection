# Dump-Detection

FastAPI service for road cracks and potholes: image detection, video tracking, and
severity scoring. Runs on CPU and deploys to AWS using Terraform.

**Live API:** https://d27aph0gu4g2ie.cloudfront.net

**Interactive docs:** https://d27aph0gu4g2ie.cloudfront.net/docs

The AWS deployment is public, requires no API key, and permits all website origins.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Readiness |
| GET | `/model` | Active model, class labels, and upload limits |
| POST | `/detect` | Image detection; multipart `file` and optional `confidence` |
| POST | `/detect/video/jobs` | Submit a video; returns `job_id` and `status_url` |
| GET | `/detect/video/jobs/{job_id}` | Poll for results or errors |
| POST | `/detect/video` | Synchronous video processing, local use only |

Image results contain bounding boxes in pixels, confidence, class labels, and severity
scores. Video results also include unique damage tracks and class/severity totals.
Classes are D00 (longitudinal crack), D10 (transverse crack), D20 (alligator crack), and
D40 (pothole). This is road-damage detection, not garbage/dumping detection.

```powershell
curl.exe -X POST https://d27aph0gu4g2ie.cloudfront.net/detect -F "file=@samples/road_02.jpg"
curl.exe -X POST https://d27aph0gu4g2ie.cloudfront.net/detect/video/jobs -F "file=@clip.mp4" -F "frame_stride=2"
```

Poll the returned video `status_url` until `status` is `completed` or `failed`. Successful
results are in `result`. Results expire after one hour or a server restart; at most 100
jobs are retained. Uploaded videos are deleted after processing.

Limits:

- Images: JPEG/PNG/WebP, 10 MiB, 20 megapixels.
- Videos: MP4/MOV/AVI/MKV, 50 MiB, 1,800 decoded frames, 2,073,600 pixels/frame.
- `confidence`: 0–1, default 0.25. Video `frame_stride`: 1–10, default 2.
- One inference at a time; busy requests return 429. AWS also limits POSTs to 10/minute/IP.
- Invalid media returns 400/415, oversized media 413, and invalid fields 422.

See [AWS deployment and JavaScript integration examples](infra/README.md).

## Run locally

```powershell
git clone https://github.com/Saad-i514/Dump-Detection.git
cd Dump-Detection
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Place trained weights at `models/road_damage_best.pt` (or `.onnx`). ONNX takes precedence
if both exist. The API refuses to start without trained weights and the expected class
order. Model binaries are excluded from Git.

The currently deployed interim weights can be obtained from their original source:

```powershell
Invoke-WebRequest 'https://github.com/oracl4/RoadDamageDetection/raw/refs/heads/main/models/YOLOv8_Small_RDD.pt' -OutFile 'models/road_damage_best.pt'
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Open http://localhost:8000/docs. Optional environment variables:

- `RDD_API_KEY`: require `X-API-Key` for model/detection/job endpoints; health stays public.
- `RDD_CORS_ORIGINS`: comma-separated website origins, or `*` for public access.
- `RDD_ASYNC_VIDEO_ONLY=1`: require video jobs instead of synchronous video requests.

Run tests with `.\.venv\Scripts\python.exe -m pytest tests/ -q`. The retained sample image
is used by real-model integration tests. Install weights before running the full suite.

## Repository

- `api/`: HTTP routes, validation, and background video jobs.
- `src/core/`: model inference, severity scoring, and video tracking.
- `src/utils/`: model resolution and shared constants.
- `tests/`: API and model/core tests.
- `infra/`: Terraform, server bootstrap, and deployment guide.
- `scripts/`: deployment packaging and AWS deployment command.

## Attribution and limitations

Core detection, severity, and tracking code derives from
[IhsanKT/road-damage-detector](https://github.com/IhsanKT/road-damage-detector). Original
Git history is retained. The interim model is `YOLOv8_Small_RDD.pt` from
[oracl4/RoadDamageDetection](https://github.com/oracl4/RoadDamageDetection). Its repository
does not publish a model licence; redistribution rights are unclear. Use appropriately
licensed/self-trained weights for redistribution.

Severity scores are ranking heuristics, not validated engineering assessments. Detection
quality depends on the training data and camera angle. AWS is a single-server deployment;
jobs are not durable across restarts. Public requests consume AWS credits and can incur costs.
