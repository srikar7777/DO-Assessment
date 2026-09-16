# myservice — Image Thumbnail API

A production-ready REST API that accepts one or more image uploads, resizes them into
thumbnails using named presets or custom dimensions while always preserving the original
aspect ratio, and returns structured metadata plus retrievable files for every output.
Uploads in a batch are processed concurrently, and CPU-bound image encoding is pushed off
the event loop so the service stays responsive under simultaneous client requests.

---

## Table of contents

- [Architecture](#architecture)
- [Request lifecycle](#request-lifecycle)
- [Concurrency model](#concurrency-model)
- [Data model](#data-model)
- [Error handling flow](#error-handling-flow)
- [API endpoints](#api-endpoints)
- [Example request and response](#example-request-and-response)
- [Quick start](#quick-start)
- [Running with Docker](#running-with-docker)
- [Environment variables](#environment-variables)
- [Testing](#testing)
- [CI/CD pipeline](#cicd-pipeline)
- [Deploying to DigitalOcean](#deploying-to-digitalocean)
- [Architecture decisions and trade-offs](#architecture-decisions-and-trade-offs)
- [Known gaps for production](#known-gaps-for-production)

---

## Architecture

### The one-minute version

Four boxes. A request enters at the left and the work fans out to two places at the right.

```
                 ┌──────────────┐
  client  ──────▶│    ROUTES    │   speaks HTTP. parses the upload,
   POST          │  app/routes  │   returns the response. no logic.
  an image       └──────┬───────┘
                        │ calls
                        ▼
                 ┌──────────────┐
                 │   SERVICE    │   does the actual work: validate the file,
                 │ app/services │   resize with Pillow, save the results.
                 └──┬────────┬──┘
           writes   │        │   writes
          metadata  │        │   pixels
                    ▼        ▼
            ┌───────────┐  ┌───────────┐
            │ POSTGRES  │  │  SPACES   │
            │  rows     │  │  files    │
            └───────────┘  └───────────┘
```

**The one idea worth remembering: metadata and pixels are stored separately.** The database
holds small rows describing each image and thumbnail (dimensions, size, format, status).
The image bytes themselves live in object storage, and the database row just keeps a
`storage_key` pointing at them. Databases are bad at holding megabytes of binary; object
storage is cheap and scales without limit.

Two consequences fall out of that split, and they are the two things worth saying out loud:

1. **Routes never touch the database, and the service never touches HTTP.** The service
   raises plain domain errors like `ImageTooLarge`; the route is what decides that means
   a `413`. So the business logic can be tested without a web server, and swapping the
   transport would not touch it.
2. **Storage is an interface, not a vendor.** `StorageBackend` has two implementations,
   local disk for development and S3 for production. Which one you get is an environment
   variable, so running locally needs no cloud account and no code changes.

Resizing is CPU-bound and Pillow releases the GIL, so each resize runs in a worker thread
rather than on the event loop, and a batch upload processes its files concurrently. That's
the whole concurrency story — [details below](#concurrency-model).

### The detailed version

The service is a layered FastAPI application. Each layer may only call the layer beneath
it: routes never touch the database, and services never import anything HTTP-specific.

<details>
<summary>Expand the full deployment and layer diagram</summary>

```
                                   ┌─────────────────┐
                                   │     Client      │
                                   │ curl / SDK / UI │
                                   └────────┬────────┘
                                            │ HTTPS  multipart/form-data
                                            ▼
╔═══════════════════════════════════════════════════════════════════════════════╗
║                           DigitalOcean App Platform                           ║
║                        (TLS termination, DefaultIngress)                      ║
╟───────────────────────────────────────────────────────────────────────────────╢
║  Container :8000 — uvicorn, 2 workers, non-root appuser                       ║
║                                                                               ║
║  ┌─────────────────────────────────────────────────────────────────────────┐  ║
║  │ MIDDLEWARE / CROSS-CUTTING                  app/main.py, app/core        │  ║
║  │   CORSMiddleware  ·  structlog JSON logs  ·  3 global exception handlers │  ║
║  └────────────────────────────────┬────────────────────────────────────────┘  ║
║                                   ▼                                           ║
║  ┌─────────────────────────────────────────────────────────────────────────┐  ║
║  │ ROUTE LAYER — HTTP only, no business logic       app/routes/             │  ║
║  │   health.py      GET /health, GET /ready                                 │  ║
║  │   thumbnails.py  POST/GET/DELETE /v1/*                                   │  ║
║  │                                                                          │  ║
║  │   Responsibilities: parse multipart, coerce form fields into a           │  ║
║  │   validated ResizeRequest, inject dependencies, map service              │  ║
║  │   exceptions to HTTP status codes, serialize response models.            │  ║
║  └───────┬──────────────────────────────────────────┬──────────────────────┘  ║
║          │ Depends(require_api_key)                 │ Depends(get_db)         ║
║          │ Depends(enforce_rate_limit)              │ Depends(get_service)    ║
║          ▼                                          ▼                         ║
║  ┌────────────────────────┐   ┌──────────────────────────────────────────────┐║
║  │ SECURITY  core/        │   │ SERVICE LAYER — all business logic           │║
║  │  X-API-Key check       │   │ app/services/thumbnail_service.py            │║
║  │  per-IP rate limiter   │   │                                              │║
║  └────────────────────────┘   │  validate → probe → resize → store → persist │║
║                               │  ThumbnailService owns every DB write and    │║
║  ┌────────────────────────┐   │  every Pillow call. Raises domain errors,    │║
║  │ VALIDATION             │◀──│  never HTTPException.                        │║
║  │ app/models/schemas.py  │   └───────┬──────────────────────────┬───────────┘║
║  │  Pydantic v2 input /   │           │                          │            ║
║  │  output models         │           ▼                          ▼            ║
║  └────────────────────────┘   ┌───────────────────┐   ┌──────────────────────┐║
║                               │ PERSISTENCE       │   │ STORAGE ABSTRACTION  │║
║                               │ core/database.py  │   │ services/storage.py  │║
║                               │ models/thumbnail  │   │ StorageBackend proto │║
║                               │ SQLAlchemy 2.0    │   │  ├ LocalStorage      │║
║                               └─────────┬─────────┘   │  └ S3Storage         │║
║                                         │             └──────────┬───────────┘║
╚═════════════════════════════════════════│════════════════════════│════════════╝
                                          ▼                        ▼
                            ┌──────────────────────┐   ┌────────────────────────┐
                            │  SQLite (local)      │   │  Local disk (dev)      │
                            │  PostgreSQL (prod)   │   │  DO Spaces / S3 (prod) │
                            │  metadata only       │   │  image bytes only      │
                            └──────────────────────┘   └────────────────────────┘
```

</details>

### Module map

```
app/
├── main.py                       FastAPI app, lifespan, CORS, exception handlers
├── core/
│   ├── config.py                 pydantic-settings BaseSettings + production guards
│   ├── database.py               engine, SessionLocal, Base, get_db() dependency
│   ├── logging.py                structlog: JSONRenderer (prod) / ConsoleRenderer (dev)
│   └── security.py               API-key dependency, per-IP rate limiter
├── models/
│   ├── thumbnail.py              SQLAlchemy: Image, Thumbnail (UUID PKs)
│   └── schemas.py                Pydantic v2 request/response contracts
├── routes/
│   ├── health.py                 liveness + readiness
│   └── thumbnails.py             upload, list, get, download, delete, presets, stats
└── services/
    ├── thumbnail_service.py      business logic, Pillow pipeline, DB operations
    └── storage.py                StorageBackend protocol, Local + S3 implementations
```

---

## Request lifecycle

### The one-minute version

What happens when you `POST /v1/images` with one file and three presets:

```
  1. check the API key and the rate limit          → 401 / 429 if either fails
  2. validate the form fields with Pydantic        → 422 if anything is out of range
  3. read the upload, confirm it's really an image → 400 if not, 413 if too big
  4. save the original to object storage, write the Image row  (status = processing)
  5. for each of the 3 presets:
        resize in a worker thread, upload the result, write a Thumbnail row
  6. mark the Image "ready" and return 201 with all the metadata
```

Steps 1–3 are cheap and happen on the event loop. Step 5 is the expensive part, so it runs
off the event loop in threads. If a batch has several files, step 3 onward runs for all of
them concurrently and one bad file fails on its own without taking down the others.

### The detailed version

The full path of `POST /v1/images` with three presets. Note where control crosses from the
async event loop into the worker thread pool and back.

<details>
<summary>Expand the full sequence diagram</summary>

```
CLIENT                ROUTE                 SERVICE              THREAD POOL      STORAGE / DB
  │                     │                      │                      │                 │
  │ POST /v1/images     │                      │                      │                 │
  │ multipart: files[]  │                      │                      │                 │
  │ + presets=s,m,l     │                      │                      │                 │
  ├────────────────────▶│                      │                      │                 │
  │                     │                                                                │
  │              ┌──────┴───────┐  (1) require_api_key  → 401 if X-API-Key wrong          │
  │              │ DEPENDENCIES │  (2) enforce_rate_limit → 429 if over quota             │
  │              └──────┬───────┘  (3) get_db → Session   (4) get_service → Service       │
  │                     │                                                                │
  │              ┌──────┴────────────────┐                                               │
  │              │ (5) BOUNDARY VALIDATE │  ResizeRequest.model_validate(form)            │
  │              │     Pydantic v2       │  presets non-empty · 1 ≤ w,h ≤ 4096            │
  │              └──────┬────────────────┘  custom ⇔ dimensions · 1 ≤ quality ≤ 100       │
  │                     │                    │                                           │
  │◀ 422 structured ────┤ ValidationError → RequestValidationError → global handler       │
  │  {error, details[]} │                    │                                           │
  │                     │                    │                                           │
  │                     │ (6) await file.read() for each upload                           │
  │                     ├───────────────────▶│                                           │
  │                     │  create_images()   │                                           │
  │                     │                    │ (7) guard: len(files) ≤ MAX_IMAGES → 413   │
  │                     │                    │                                           │
  │                     │                    │ (8) asyncio.gather(return_exceptions=True) │
  │                     │                    │     one _process_one() task per file       │
  │                     │        ┌───────────┴────────────┐                               │
  │                     │        │  per file, in parallel │                               │
  │                     │        │                        │                               │
  │                     │        │ (9) _validate_upload   │  content-type allowlist       │
  │                     │        │     size ≤ MAX_BYTES   │  → InvalidImage / TooLarge    │
  │                     │        │                        │                               │
  │                     │        │ (10) probe dimensions ─┼─────────▶│ PIL.verify()       │
  │                     │        │      to_thread.run_sync│          │ exif_transpose     │
  │                     │        │◀───────────────────────┼──────────┤ (w, h, format)     │
  │                     │        │                        │          │                    │
  │                     │        │ (11) save original ────┼──────────┼────────▶ PUT       │
  │                     │        │      to_thread.run_sync│          │   originals/{id}/  │
  │                     │        │                        │          │                    │
  │                     │        │ (12) gather over presets — 3 concurrent renders        │
  │                     │        │      to_thread.run_sync┼─────────▶│ thumbnail(box,     │
  │                     │        │                        │          │   LANCZOS)         │
  │                     │        │                        │          │ aspect preserved,  │
  │                     │        │                        │          │ never upscaled     │
  │                     │        │◀───────────────────────┼──────────┤ encoded bytes      │
  │                     │        │                        │          │                    │
  │                     │        │ (13) save each thumb ──┼──────────┼────────▶ PUT       │
  │                     │        │                        │          │  thumbnails/{id}/  │
  │                     │        └───────────┬────────────┘                               │
  │                     │                    │                                           │
  │                     │                    │ (14) partition results:                    │
  │                     │                    │      ok → created[]   raised → failed++    │
  │                     │                    │                                           │
  │                     │                    │ (15) ONE transaction for the whole batch   │
  │                     │                    ├──────────────────────────▶ add_all()       │
  │                     │                    │                            commit()        │
  │                     │                    │                            refresh() ×N    │
  │                     │                    │   on SQLAlchemyError: rollback() + re-raise │
  │                     │◀───────────────────┤ (created[], failed)                        │
  │                     │                    │                                           │
  │              ┌──────┴────────────────┐                                               │
  │              │ (16) MAP TO SCHEMAS   │  to_image_detail(): ORM → ImageDetailOut       │
  │              │  _as_utc() normalizes │  download_url = Spaces CDN URL if configured,  │
  │              │  naive SQLite stamps  │  else the local /v1/.../file route             │
  │              └──────┬────────────────┘                                               │
  │◀ 201 UploadResponse │                                                                │
  │  {uploaded, failed, │                                                                │
  │   images[]}         │                                                                │
```

</details>

### Retrieval path

```
GET /v1/images/{id}          → service.get_image()  → selectinload(thumbnails)
                               → 404 ImageNotFoundError if absent
                               → ImageDetailOut (metadata + every thumbnail + URLs)

GET /v1/images?status=ready  → service.list_images(filters, limit, offset)
  &content_type=image/jpeg     → COUNT(*) query for `total` (full match count)
  &limit=20&offset=0           → SELECT … ORDER BY received_at DESC LIMIT/OFFSET
                               → ImageListResponse{total, limit, offset, results[]}

GET /v1/thumbnails/{id}/file → service.get_thumbnail() → read_bytes() off-loop
                               → Response(bytes, media_type, Content-Disposition)
```

---

## Concurrency model

FastAPI runs a single-threaded event loop per uvicorn worker. Image decoding and encoding
are CPU-bound and would block that loop, so every Pillow call and every blocking storage
write is dispatched to AnyIO's worker-thread pool. The loop stays free to accept new
connections while resizing happens.

```
uvicorn worker (×2 in the container)
│
├── asyncio event loop  ── never blocked ──────────────────────────────────────┐
│     │                                                                        │
│     ├── request A: POST /v1/images (5 files)                                  │
│     │     └── asyncio.gather ─┬─ _process_one(file 1) ─┐                      │
│     │                         ├─ _process_one(file 2) ─┤                      │
│     │                         ├─ _process_one(file 3) ─┼── await to_thread ──▶│
│     │                         ├─ _process_one(file 4) ─┤                      │
│     │                         └─ _process_one(file 5) ─┘                      │
│     │                                                                        │
│     ├── request B: GET /v1/images      ── served immediately, no blocking ──  │
│     └── request C: GET /health         ── served immediately, no blocking ──  │
│                                                                              │
└── AnyIO worker thread pool ◀─────────────────────────────────────────────────┘
      ├── thread 1: PIL decode → LANCZOS resize → encode  (releases the GIL)
      ├── thread 2: PIL decode → LANCZOS resize → encode
      └── thread N: storage.save() / storage.load()
```

Three properties fall out of this design:

1. **Batch parallelism.** A 5-file upload with 3 presets issues up to 15 independent
   render tasks rather than 15 sequential ones.
2. **Failure isolation.** `asyncio.gather(..., return_exceptions=True)` means one corrupt
   file is counted in `failed` while every valid file still returns `ready`.
3. **Transactional integrity.** All rendering finishes before a single `commit()` writes
   the batch, so the database never contains a row whose bytes were never stored.

Pillow releases the GIL during encode/decode, so thread-based parallelism gives genuine
multi-core throughput here rather than just interleaving.

---

## Data model

```
┌────────────────────────────────┐          ┌─────────────────────────────────┐
│ images                         │          │ thumbnails                      │
├────────────────────────────────┤          ├─────────────────────────────────┤
│ id                UUID  PK     │◀────┐    │ id              UUID  PK        │
│ original_filename VARCHAR(255) │     │    │ image_id        UUID  FK  [idx] │
│ content_type      VARCHAR(100) │     └────┤ preset          VARCHAR(32)[idx]│
│ size_bytes        INTEGER      │  1:N     │ width           INTEGER         │
│ width             INTEGER      │          │ height          INTEGER         │
│ height            INTEGER      │          │ format          VARCHAR(16)     │
│ storage_key       VARCHAR(512) │          │ size_bytes      INTEGER         │
│ status            VARCHAR(32)  │  [idx]   │ storage_key     VARCHAR(512)    │
│ error_message     TEXT NULL    │          │ quality         INTEGER         │
│ received_at       TIMESTAMPTZ  │  [idx]   │ status          VARCHAR(32)[idx]│
│ updated_at        TIMESTAMPTZ  │          │ created_at      TIMESTAMPTZ[idx]│
└────────────────────────────────┘          └─────────────────────────────────┘
        ON DELETE CASCADE ────────────────────────────▶ thumbnails removed with parent
```

Every column used for filtering or ordering carries an index. Primary keys are UUIDs
generated by the application, not auto-increment integers, so identifiers are
non-enumerable and can be produced before the row is written.

### Storage layout

```
STORAGE_DIR (or the Spaces bucket)
├── originals/
│   └── {image_uuid}/
│       └── vacation.jpg                 sanitized filename, no path traversal
└── thumbnails/
    └── {image_uuid}/
        ├── {thumb_uuid}.jpg             small
        ├── {thumb_uuid}.jpg             medium
        └── {thumb_uuid}.jpg             large
```

---

## Error handling flow

```
                          exception raised anywhere
                                     │
        ┌────────────────────────────┼─────────────────────────────┐
        ▼                            ▼                             ▼
  service errors              RequestValidationError         SQLAlchemyError
  (domain layer)              (Pydantic boundary)            (any DB failure)
        │                            │                             │
        │ caught in route            │ global handler              │ global handler
        ▼                            ▼                             ▼
 ImageNotFoundError → 404     422 + details[]              rollback() then 500
 InvalidImageError  → 400     [{field, message, type}]     "A database error occurred"
 PayloadTooLarge    → 413     mapped to plain dicts by     full traceback logged,
 StorageError       → 503     hand so JSONResponse can     nothing internal returned
                              always serialize them
        │                            │                             │
        └────────────────────────────┴─────────────────────────────┘
                                     │
                          anything else unhandled
                                     ▼
                         Exception catch-all → 500
                    logs error_type + traceback internally,
                    returns {"error": "internal_error", …}
```

No stack trace, driver message, or connection string ever reaches a client. Every failure
is logged internally with named structlog fields and answered with a safe envelope.

---

## API endpoints

| Method | Path | Auth | Description |
|--------|------|:----:|-------------|
| `GET` | `/` | — | Service metadata and documentation links |
| `GET` | `/health` | — | Liveness probe. No database call. Returns name, version, environment, timestamp |
| `GET` | `/ready` | — | Readiness probe. Runs `SELECT 1`; returns **503** if the database is unreachable |
| `POST` | `/v1/images` | key | Upload one or more images and generate a thumbnail per requested preset |
| `GET` | `/v1/images` | — | List images. Filter by `status` and `content_type`; paginate with `limit` and `offset` |
| `GET` | `/v1/images/{id}` | — | Image metadata together with all of its thumbnails |
| `GET` | `/v1/images/{id}/file` | — | Download the original image bytes |
| `GET` | `/v1/thumbnails/{id}/file` | — | Download a generated thumbnail |
| `DELETE` | `/v1/images/{id}` | key | Delete an image, its thumbnails, and every stored object |
| `GET` | `/v1/presets` | — | List supported presets and their dimensions |
| `GET` | `/v1/stats` | — | Aggregate counts of images, thumbnails, and bytes stored |
| `GET` | `/docs` | — | Interactive OpenAPI documentation |

`key` means the `X-API-Key` header is required whenever `API_KEY` is configured. It is
mandatory in production and optional in local development.

### Upload parameters

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `files` | file[] | required | 1–`MAX_IMAGES_PER_REQUEST`; each ≤ `MAX_UPLOAD_BYTES` |
| `presets` | string | `medium` | Comma-separated: `small`, `medium`, `large`, `custom` |
| `width` | int | — | 1–4096. Only valid with `custom` |
| `height` | int | — | 1–4096. Only valid with `custom` |
| `output_format` | string | source format | `jpeg`, `png`, or `webp` |
| `quality` | int | `85` | 1–100. Applies to JPEG and WebP |

Presets fit the image inside a bounding box: `small` 150×150, `medium` 400×400,
`large` 1024×1024. Aspect ratio is always preserved and images are never upscaled.

---

## Example request and response

Upload one image and generate three thumbnails:

```bash
curl -X POST http://localhost:8000/v1/images \
  -H "X-API-Key: $API_KEY" \
  -F "files=@vacation.jpg" \
  -F "presets=small,medium,large"
```

Response `201 Created` — a 1920×1080 source, with each output preserving the 16:9 ratio:

```json
{
  "uploaded": 1,
  "failed": 0,
  "images": [
    {
      "id": "7c615c16-0657-49cf-82b5-ec901f51bc76",
      "original_filename": "vacation.jpg",
      "content_type": "image/jpeg",
      "size_bytes": 33269,
      "width": 1920,
      "height": 1080,
      "status": "ready",
      "error_message": null,
      "received_at": "2026-09-16T18:10:06.727386Z",
      "updated_at": "2026-09-16T18:10:06.727388Z",
      "download_url": "/v1/images/7c615c16-0657-49cf-82b5-ec901f51bc76/file",
      "thumbnails": [
        {
          "id": "f142ceb7-2735-4ff2-bf72-e81c527462ee",
          "image_id": "7c615c16-0657-49cf-82b5-ec901f51bc76",
          "preset": "small",
          "width": 150,
          "height": 84,
          "format": "jpeg",
          "size_bytes": 377,
          "quality": 85,
          "status": "ready",
          "created_at": "2026-09-16T18:10:06.759773Z",
          "download_url": "/v1/thumbnails/f142ceb7-2735-4ff2-bf72-e81c527462ee/file"
        },
        {
          "id": "7720b889-50c1-4486-8e08-dc20d92b46eb",
          "image_id": "7c615c16-0657-49cf-82b5-ec901f51bc76",
          "preset": "medium",
          "width": 400,
          "height": 225,
          "format": "jpeg",
          "size_bytes": 850,
          "quality": 85,
          "status": "ready",
          "created_at": "2026-09-16T18:10:06.760138Z",
          "download_url": "/v1/thumbnails/7720b889-50c1-4486-8e08-dc20d92b46eb/file"
        },
        {
          "id": "ee2b9898-fc8b-4d3c-a643-ba12307440ba",
          "image_id": "7c615c16-0657-49cf-82b5-ec901f51bc76",
          "preset": "large",
          "width": 1024,
          "height": 576,
          "format": "jpeg",
          "size_bytes": 3743,
          "quality": 85,
          "status": "ready",
          "created_at": "2026-09-16T18:10:06.760436Z",
          "download_url": "/v1/thumbnails/ee2b9898-fc8b-4d3c-a643-ba12307440ba/file"
        }
      ]
    }
  ]
}
```

### More examples

```bash
# Custom dimensions — height is derived from the aspect ratio
curl -X POST http://localhost:8000/v1/images \
  -F "files=@photo.jpg" -F "presets=custom" -F "width=600" -F "height=600"

# Batch upload, WebP output at quality 90
curl -X POST http://localhost:8000/v1/images \
  -F "files=@a.jpg" -F "files=@b.png" -F "files=@c.webp" \
  -F "presets=small,medium" -F "output_format=webp" -F "quality=90"

# Filter and paginate
curl "http://localhost:8000/v1/images?status=ready&content_type=image/jpeg&limit=10&offset=0"

# Download a thumbnail
curl -o thumb.jpg http://localhost:8000/v1/thumbnails/f142ceb7-2735-4ff2-bf72-e81c527462ee/file

# Delete an image and everything derived from it
curl -X DELETE -H "X-API-Key: $API_KEY" \
  http://localhost:8000/v1/images/7c615c16-0657-49cf-82b5-ec901f51bc76
```

A validation failure returns `422` with one entry per offending field:

```json
{
  "error": "validation_error",
  "message": "Request validation failed",
  "details": [
    {
      "field": "presets.0",
      "message": "Input should be 'small', 'medium', 'large' or 'custom'",
      "type": "enum"
    }
  ]
}
```

---

## Quick start

**Requirements:** Python 3.11+, and Docker (or OrbStack) if you want the container path.

```bash
# 1. Clone and enter the project
git clone <your-repo-url>
cd myservice

# 2. Create the virtualenv and install dependencies
make install
#    equivalent to:
#    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. Create your local configuration
cp .env.example .env
#    The defaults work as-is: SQLite, local storage, no API key required.

# 4. Run the service with auto-reload
make dev
```

The API is now at `http://localhost:8000`, with interactive documentation at
`http://localhost:8000/docs`.

Verify it is up:

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"myservice","version":"0.1.0", ...}

curl http://localhost:8000/ready
# {"status":"ready", ..., "database":"ok"}
```

> **If your system Python is older than 3.11**, the application will not run natively —
> it uses `X | None` annotations evaluated at runtime by Pydantic. Either install 3.11
> (`brew install python@3.11`, then `make install PYTHON=python3.11`) or use the Docker
> path below, which pins 3.11 for you.

---

## Running with Docker

```bash
make docker-run          # docker compose up --build  → http://localhost:8000
make docker-stop         # docker compose down
make docker-build        # build the image only, tagged myservice:local
```

Compose persists uploads in a named `storage_data` volume, so thumbnails survive a
restart. Every setting is passed through from your environment or `.env`, and the
container healthcheck mirrors the one baked into the image, so `docker compose ps`
reports genuine application health rather than just "process running".

Build for DigitalOcean's linux/amd64 servers (required on Apple Silicon, which would
otherwise produce an arm64 image that cannot run on DO):

```bash
docker buildx build --platform linux/amd64 \
  --tag registry.digitalocean.com/<REGISTRY_NAME>/myservice:latest \
  --tag registry.digitalocean.com/<REGISTRY_NAME>/myservice:$(git rev-parse --short HEAD) \
  --push .

# or simply:
make docker-build-prod REGISTRY_NAME=<your-registry>
```

---

## Environment variables

Every setting is read from the environment through pydantic-settings. Nothing is
hardcoded. Copy `.env.example` to `.env` to get started.

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_NAME` | `myservice` | Service name reported by health endpoints and logs |
| `APP_VERSION` | `0.1.0` | Version string reported by health endpoints |
| `APP_PORT` | `8000` | Port for local development and the compose host mapping |
| `ENVIRONMENT` | `development` | `development`, `staging`, or `production`. Selects the log renderer and activates production guards |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `DATABASE_URL` | `sqlite:///./myservice.db` | SQLAlchemy URL. Use `postgresql://…` in production |
| `STORAGE_BACKEND` | `local` | `local` for disk, `s3` for Spaces or S3 |
| `STORAGE_DIR` | `./storage` | Root directory when the backend is `local` |
| `S3_ENDPOINT_URL` | *(empty)* | e.g. `https://nyc3.digitaloceanspaces.com` |
| `S3_REGION` | `nyc3` | Region for the S3-compatible client |
| `S3_BUCKET` | *(empty)* | Bucket or Space name |
| `S3_ACCESS_KEY_ID` | *(empty)* | **Secret.** Spaces access key |
| `S3_SECRET_ACCESS_KEY` | *(empty)* | **Secret.** Spaces secret key |
| `S3_PUBLIC_BASE_URL` | *(empty)* | CDN base URL. When set, `download_url` points here instead of the API |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:8000` | Comma-separated allowed origins. `*` is rejected in production |
| `API_KEY` | *(empty)* | **Secret.** Required for mutating endpoints. Mandatory in production |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `60` | Per-IP limit. `0` disables it |
| `MAX_UPLOAD_BYTES` | `10485760` | Per-file size cap (10 MB) |
| `MAX_IMAGES_PER_REQUEST` | `10` | Per-request file count cap |
| `DEFAULT_THUMBNAIL_QUALITY` | `85` | Default JPEG/WebP quality when the caller omits it |

**Production guards.** When `ENVIRONMENT=production`, the service refuses to start if
`API_KEY` is empty, if `CORS_ORIGINS` is `*`, if `DATABASE_URL` still points at SQLite, or
if `STORAGE_BACKEND=s3` without complete credentials. Misconfiguration fails at boot
rather than silently serving traffic with authentication disabled.

---

## Testing

```bash
make test              # python -m pytest tests/ -v
make test-coverage     # adds --cov=app --cov-report=term-missing
make ci                # exactly what CI runs: lint + coverage with --cov-fail-under=70
make test-docker       # run the suite inside a Python 3.11 container
make lint              # ruff check app/ tests/
make lint-fix          # ruff check app/ tests/ --fix
```

The suite is **49 tests, all passing, with zero warnings** (verified under
`-W error::DeprecationWarning`).

Tests never touch your real database or storage. `tests/conftest.py` sets `DATABASE_URL`
and `STORAGE_DIR` to a dedicated test database and a throwaway directory *before* any
application module is imported, overrides the `get_db` dependency, and uses an `autouse`
fixture that creates all tables before each test and drops them afterwards — so every test
starts from a clean database.

Coverage by category:

| Category | Examples |
|----------|----------|
| Happy path | Single upload, batch upload, one thumbnail per preset |
| Aspect ratio | 1200×800 → 150×100 in the `small` box; small sources are never upscaled |
| Server-generated fields | `id`, `status`, and `received_at` are ignored when supplied by the caller |
| Validation boundaries | One test per rule: empty presets, unknown preset, `custom` without dimensions, dimensions without `custom`, width 0, width > 4096, quality 0, quality > 100, unknown format |
| Missing fields | Absent `files`; `presets` defaulting to `medium` |
| Rejected input | Non-image content type, corrupt bytes, too many files |
| Not found | 404 for image, thumbnail, download, and delete |
| Listing | Empty list with `total: 0`; populated list with correct `total` |
| Filtering | By `status` and by `content_type` |
| Pagination | `limit` caps `results` while `total` reflects every match; `offset` shifts the window |
| Concurrency | A 5-file batch, plus 16 parallel threaded reads |
| Partial failure | One corrupt file in a batch yields `uploaded: 1, failed: 1` |
| Health | Liveness shape, readiness OK, and readiness 503 when the database raises |

---

## CI/CD pipeline

`.github/workflows/ci.yml` defines three sequential jobs.

```
   push / pull_request
           │
           ▼
┌──────────────────────┐
│ JOB 1 — test         │  every push and PR, on all branches
│  checkout@v4         │
│  setup-python@v5     │  3.11, cache: pip
│  pip install -r …    │
│  ruff check app/ tests/
│  pytest --cov=app --cov-report=term-missing
│  pytest --cov-fail-under=70
└──────────┬───────────┘
           │ needs: test    +    push to main only
           ▼
┌──────────────────────┐
│ JOB 2 — build        │
│  doctl via digitalocean/action-doctl@v2
│  doctl registry login --expiry-seconds 600
│  docker buildx build --platform linux/amd64
│      --tag …/myservice:latest
│      --tag …/myservice:${{ github.sha }}     ← traceability + rollback
│      --push
└──────────┬───────────┘
           │ needs: build   +    push to main only
           ▼
┌──────────────────────┐
│ JOB 3 — deploy       │
│  resolve APP_ID from `doctl apps list` (fails loudly if not found)
│  doctl apps create-deployment $APP_ID
│  poll 20 × 15s → exit 0 on ACTIVE, exit 1 on ERROR or CANCELED
│  APP_URL=$(doctl apps get $APP_ID --format DefaultIngress --no-header)
│  curl --fail "$APP_URL/health"        ← DefaultIngress already has https://
└──────────────────────┘
```

Both `latest` and the commit SHA are pushed, so any past commit can be redeployed by tag.
The deploy job polls for up to five minutes instead of sleeping a fixed interval, because
App Platform routinely takes longer than 30 seconds to reach `ACTIVE`.

**Required GitHub secrets:**

| Secret | Purpose |
|--------|---------|
| `DIGITALOCEAN_ACCESS_TOKEN` | DO API token with read + write scope |
| `REGISTRY_NAME` | Your DigitalOcean container registry name |

---

## Deploying to DigitalOcean

```bash
# 1. Authenticate
doctl auth init

# 2. Create a container registry (once)
doctl registry create <your-registry-name>

# 3. Build and push the linux/amd64 image
doctl registry login
make docker-build-prod REGISTRY_NAME=<your-registry-name>

# 4. Edit .do/app.yaml — set the real DATABASE_URL, API_KEY, and CORS_ORIGINS
#    then create the app
doctl apps create --spec .do/app.yaml

# 5. Find the live URL
doctl apps list --format ID,Spec.Name,DefaultIngress
```

`.do/app.yaml` pins `instance_size_slug: basic-xxs`, `http_port: 8000` to match the
container, and a `/health` check with a 10-second initial delay, 30-second period,
10-second timeout, and 3-failure threshold. `DATABASE_URL`, `API_KEY`, and the Spaces
credentials are marked `type: SECRET`; every other variable is listed in plain sight.

After the first deploy, subsequent pushes to `main` deploy automatically through the
pipeline above. `deploy_on_push` is disabled in the spec so App Platform's own trigger
does not race the GitHub Actions job.

---

## Architecture decisions and trade-offs

**Synchronous processing instead of a job queue.** Thumbnails are generated during the
request, so a caller gets finished results in one round-trip with no queue, broker, or
worker fleet to operate. The cost is that request latency scales with image size and
preset count, and a large batch occupies worker threads for its duration. The service
layer is already structured so that moving `_process_one` behind a queue would not change
the route layer at all — the `status` field on both tables exists precisely to support a
future `pending → ready` transition.

**Metadata in the database, bytes in object storage.** Rows stay small and queryable while
pixels live somewhere built for them. The trade-off is that the two can drift: a failed
delete leaves an orphaned object. A reconciliation job is the standard remedy and is
listed as a gap below.

**Threads rather than processes for CPU work.** Pillow releases the GIL during encode and
decode, so `anyio.to_thread` gives real parallelism without the memory cost and
serialization overhead of a process pool. This holds for image codecs specifically; pure
Python CPU work would need processes instead.

**Aspect ratio via `Image.thumbnail()` with a bounding box.** Presets define a box the
image must fit inside, never exact output dimensions, so a 16:9 source stays 16:9 and is
never upscaled beyond its original size. Callers who need exact dimensions with cropping
are not served today — that is a deliberate scope choice, and `cover`/`contain` fit modes
are the natural next feature.

**Strict input validation with `extra="forbid"`.** A typo like `widht=200` produces a loud
422 rather than being silently ignored, which is the behaviour an API consumer debugging
at 2am actually wants. It does make the API less forgiving of clients that send extra
fields.

**One transaction per batch.** All rendering completes before a single `commit()`, so the
database never references bytes that were never written. The trade-off is that a database
failure discards an entire batch of completed work rather than saving what succeeded.

**Config validation at startup.** Production guards refuse to boot with SQLite, a missing
`API_KEY`, or wildcard CORS. A misconfigured deploy fails fast and visibly instead of
quietly serving unauthenticated traffic.

**UUID primary keys.** Identifiers are non-enumerable and can be generated before insert,
which matters for building storage keys. They index slightly less efficiently than
sequential integers, which is not a concern at this scale.

---

## Known gaps for production

**Storage durability.** `.do/app.yaml` ships with `STORAGE_BACKEND=local`, and App
Platform containers have ephemeral disks — uploads are lost on every redeploy and restart,
and two instances would not see each other's files. The `S3Storage` backend is implemented
and wired; switching `STORAGE_BACKEND` to `s3` and filling in the Spaces credentials is the
single most important change before real use.

**No schema migrations.** `Base.metadata.create_all()` creates missing tables but never
alters existing ones. Alembic, run as a pre-deploy job, is needed before the first schema
change reaches production.

**Rate limiting is per-process.** The limiter lives in memory, so it resets on restart and
each instance counts independently. With N instances the effective limit is N times the
configured value. Redis-backed limiting fixes both problems.

**Single shared API key.** There is no per-tenant identity, no key rotation, no scoping,
and no usage attribution. A real product needs tenant records, hashed keys, and per-key
quotas — at which point `/v1/stats` becomes per-customer rather than global.

**Downloads buffer fully in memory.** `read_bytes()` loads an entire file before
responding. Presigned Spaces URLs, or at minimum `StreamingResponse`, should replace this
for large originals.

**Error envelope is not fully uniform.** Validation and server errors return
`{error, message, details}`, while `HTTPException` responses return FastAPI's default
`{"detail": …}`. A dedicated `HTTPException` handler would unify the two shapes.

**No observability beyond logs.** There are no metrics, no traces, and no request-ID
correlation. Prometheus counters for upload volume, resize duration, and failure rate,
plus OpenTelemetry spans, are the next layer.

**No image safety limits beyond byte size.** A decompression-bomb image can be small on
disk and enormous in memory. Pillow's `MAX_IMAGE_PIXELS` guard and an explicit
megapixel ceiling should be enforced before decoding.

**Offset pagination.** `LIMIT/OFFSET` degrades on large tables and can skip or repeat rows
under concurrent writes. Cursor pagination keyed on `received_at` scales properly.

**Single instance, no autoscaling.** `instance_count: 1` means a deploy is a brief
outage and there is no headroom for traffic spikes.
