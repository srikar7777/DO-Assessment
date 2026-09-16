"""Tests for the image upload, resize and retrieval endpoints."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from tests.conftest import make_image_bytes

UPLOAD_URL = "/v1/images"


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp that may end in 'Z' or an explicit offset."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def upload(
    client: TestClient,
    data: bytes | None = None,
    filename: str = "sample.jpg",
    content_type: str = "image/jpeg",
    **form: object,
) -> object:
    """Post a single image to the upload endpoint."""
    payload = make_image_bytes() if data is None else data
    return client.post(
        UPLOAD_URL,
        files=[("files", (filename, payload, content_type))],
        data=form or {"presets": "medium"},
    )


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_upload_single_image_returns_201(client: TestClient, sample_image: bytes) -> None:
    """A valid upload returns 201 with one processed image."""
    response = upload(client, sample_image, presets="medium")

    assert response.status_code == 201
    body = response.json()
    assert body["uploaded"] == 1
    assert body["failed"] == 0
    assert len(body["images"]) == 1


def test_upload_generates_one_thumbnail_per_preset(
    client: TestClient, sample_image: bytes
) -> None:
    """Each requested preset produces its own thumbnail."""
    body = upload(client, sample_image, presets="small,medium,large").json()

    presets = sorted(t["preset"] for t in body["images"][0]["thumbnails"])
    assert presets == ["large", "medium", "small"]


def test_thumbnail_preserves_aspect_ratio(client: TestClient) -> None:
    """A 1200x800 source fits the 150x150 small box as 150x100."""
    body = upload(client, make_image_bytes(1200, 800), presets="small").json()

    thumbnail = body["images"][0]["thumbnails"][0]
    assert thumbnail["width"] == 150
    assert thumbnail["height"] == 100


def test_custom_dimensions_preserve_aspect_ratio(client: TestClient) -> None:
    """Custom width scales height proportionally."""
    body = upload(
        client, make_image_bytes(1000, 500), presets="custom", width=300, height=300
    ).json()

    thumbnail = body["images"][0]["thumbnails"][0]
    assert thumbnail["width"] == 300
    assert thumbnail["height"] == 150


def test_thumbnail_never_upscales_small_source(client: TestClient) -> None:
    """A source smaller than the preset box keeps its original size."""
    body = upload(client, make_image_bytes(80, 60), presets="large").json()

    thumbnail = body["images"][0]["thumbnails"][0]
    assert thumbnail["width"] == 80
    assert thumbnail["height"] == 60


def test_output_format_override(client: TestClient, sample_image: bytes) -> None:
    """The caller can force a specific output format."""
    body = upload(client, sample_image, presets="small", output_format="png").json()

    assert body["images"][0]["thumbnails"][0]["format"] == "png"


def test_upload_multiple_images_in_one_request(client: TestClient) -> None:
    """A batch upload processes every file concurrently."""
    files = [
        ("files", (f"image-{index}.jpg", make_image_bytes(400, 300), "image/jpeg"))
        for index in range(5)
    ]

    response = client.post(UPLOAD_URL, files=files, data={"presets": "small"})

    assert response.status_code == 201
    body = response.json()
    assert body["uploaded"] == 5
    assert body["failed"] == 0


def test_batch_with_one_bad_file_reports_partial_failure(client: TestClient) -> None:
    """A corrupt file fails without blocking the valid files in the batch."""
    files = [
        ("files", ("good.jpg", make_image_bytes(200, 200), "image/jpeg")),
        ("files", ("bad.jpg", b"not-an-image", "image/jpeg")),
    ]

    body = client.post(UPLOAD_URL, files=files, data={"presets": "small"}).json()

    assert body["uploaded"] == 1
    assert body["failed"] == 1


# --------------------------------------------------------------------------
# Server-generated fields
# --------------------------------------------------------------------------


def test_server_generates_id_status_and_timestamps(
    client: TestClient, sample_image: bytes
) -> None:
    """id, status and received_at are produced by the server."""
    image = upload(client, sample_image, presets="small").json()["images"][0]

    assert uuid.UUID(image["id"])
    assert image["status"] == "ready"
    assert parse_utc(image["received_at"]).utcoffset() == timezone.utc.utcoffset(None)
    assert image["download_url"] == f"/v1/images/{image['id']}/file"


def test_caller_cannot_override_server_fields(client: TestClient, sample_image: bytes) -> None:
    """Server-controlled fields sent by the caller are ignored."""
    forged = str(uuid.uuid4())

    image = upload(
        client,
        sample_image,
        presets="small",
        id=forged,
        status="failed",
        received_at="1999-01-01T00:00:00+00:00",
    ).json()["images"][0]

    assert image["id"] != forged
    assert image["status"] == "ready"
    assert parse_utc(image["received_at"]).year != 1999


def test_thumbnail_ids_are_unique_per_preset(client: TestClient, sample_image: bytes) -> None:
    """Every generated thumbnail receives its own UUID."""
    thumbnails = upload(client, sample_image, presets="small,medium").json()["images"][0][
        "thumbnails"
    ]

    ids = {t["id"] for t in thumbnails}
    assert len(ids) == 2


# --------------------------------------------------------------------------
# Validation boundaries
# --------------------------------------------------------------------------


def test_empty_preset_list_rejected(client: TestClient, sample_image: bytes) -> None:
    """An empty presets value is rejected."""
    assert upload(client, sample_image, presets=" ").status_code == 422


def test_unknown_preset_rejected(client: TestClient, sample_image: bytes) -> None:
    """An unrecognised preset name is rejected."""
    assert upload(client, sample_image, presets="gigantic").status_code == 422


def test_custom_preset_without_dimensions_rejected(
    client: TestClient, sample_image: bytes
) -> None:
    """The custom preset requires width and/or height."""
    assert upload(client, sample_image, presets="custom").status_code == 422


def test_dimensions_without_custom_preset_rejected(
    client: TestClient, sample_image: bytes
) -> None:
    """Width and height are only valid alongside the custom preset."""
    assert upload(client, sample_image, presets="small", width=100).status_code == 422


def test_zero_width_rejected(client: TestClient, sample_image: bytes) -> None:
    """Width below the minimum is rejected."""
    assert upload(client, sample_image, presets="custom", width=0).status_code == 422


def test_width_above_maximum_rejected(client: TestClient, sample_image: bytes) -> None:
    """Width above 4096 pixels is rejected."""
    assert upload(client, sample_image, presets="custom", width=9000).status_code == 422


def test_quality_below_minimum_rejected(client: TestClient, sample_image: bytes) -> None:
    """Quality below 1 is rejected."""
    assert upload(client, sample_image, presets="small", quality=0).status_code == 422


def test_quality_above_maximum_rejected(client: TestClient, sample_image: bytes) -> None:
    """Quality above 100 is rejected."""
    assert upload(client, sample_image, presets="small", quality=101).status_code == 422


def test_unknown_output_format_rejected(client: TestClient, sample_image: bytes) -> None:
    """An unsupported output format is rejected."""
    assert upload(client, sample_image, presets="small", output_format="tiff").status_code == 422


def test_validation_error_shape(client: TestClient, sample_image: bytes) -> None:
    """Validation failures return the structured error envelope."""
    body = upload(client, sample_image, presets="nope").json()

    assert body["error"] == "validation_error"
    assert body["details"]
    assert set(body["details"][0]) == {"field", "message", "type"}


def test_unsupported_content_type_rejected(client: TestClient) -> None:
    """A non-image content type is rejected with 400."""
    response = upload(client, b"plain text", filename="notes.txt", content_type="text/plain")

    assert response.status_code == 400


def test_corrupt_image_rejected(client: TestClient) -> None:
    """Bytes that are not a decodable image are rejected with 400."""
    assert upload(client, b"not-an-image").status_code == 400


def test_too_many_files_rejected(client: TestClient) -> None:
    """Exceeding the per-request file limit returns 413."""
    files = [
        ("files", (f"image-{index}.jpg", make_image_bytes(50, 50), "image/jpeg"))
        for index in range(11)
    ]

    response = client.post(UPLOAD_URL, files=files, data={"presets": "small"})

    assert response.status_code == 413


# --------------------------------------------------------------------------
# Missing required fields
# --------------------------------------------------------------------------


def test_missing_files_rejected(client: TestClient) -> None:
    """A request without any file is rejected."""
    assert client.post(UPLOAD_URL, data={"presets": "small"}).status_code == 422


def test_presets_default_to_medium(client: TestClient, sample_image: bytes) -> None:
    """Omitting presets falls back to the medium preset."""
    response = client.post(
        UPLOAD_URL,
        files=[("files", ("sample.jpg", sample_image, "image/jpeg"))],
    )

    assert response.status_code == 201
    assert response.json()["images"][0]["thumbnails"][0]["preset"] == "medium"


# --------------------------------------------------------------------------
# Retrieval and not found
# --------------------------------------------------------------------------


def test_get_image_returns_detail(client: TestClient, sample_image: bytes) -> None:
    """Fetching an image returns its metadata and thumbnails."""
    image_id = upload(client, sample_image, presets="small").json()["images"][0]["id"]

    response = client.get(f"{UPLOAD_URL}/{image_id}")

    assert response.status_code == 200
    assert response.json()["id"] == image_id
    assert len(response.json()["thumbnails"]) == 1


def test_get_missing_image_returns_404(client: TestClient) -> None:
    """An unknown image id returns a structured 404."""
    response = client.get(f"{UPLOAD_URL}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert "detail" in response.json()


def test_download_original_returns_bytes(client: TestClient, sample_image: bytes) -> None:
    """The original file can be downloaded."""
    image_id = upload(client, sample_image, presets="small").json()["images"][0]["id"]

    response = client.get(f"{UPLOAD_URL}/{image_id}/file")

    assert response.status_code == 200
    assert response.content == sample_image


def test_download_thumbnail_returns_bytes(client: TestClient, sample_image: bytes) -> None:
    """A generated thumbnail can be downloaded."""
    thumbnail = upload(client, sample_image, presets="small").json()["images"][0]["thumbnails"][0]

    response = client.get(f"/v1/thumbnails/{thumbnail['id']}/file")

    assert response.status_code == 200
    assert len(response.content) == thumbnail["size_bytes"]


def test_download_missing_thumbnail_returns_404(client: TestClient) -> None:
    """An unknown thumbnail id returns 404."""
    assert client.get(f"/v1/thumbnails/{uuid.uuid4()}/file").status_code == 404


def test_delete_image_removes_it(client: TestClient, sample_image: bytes) -> None:
    """Deleting an image makes it unavailable afterwards."""
    image_id = upload(client, sample_image, presets="small").json()["images"][0]["id"]

    assert client.delete(f"{UPLOAD_URL}/{image_id}").status_code == 204
    assert client.get(f"{UPLOAD_URL}/{image_id}").status_code == 404


def test_delete_missing_image_returns_404(client: TestClient) -> None:
    """Deleting an unknown image returns 404."""
    assert client.delete(f"{UPLOAD_URL}/{uuid.uuid4()}").status_code == 404


# --------------------------------------------------------------------------
# Listing, filtering and pagination
# --------------------------------------------------------------------------


def test_list_empty_returns_zero_total(client: TestClient) -> None:
    """An empty database returns no results and a total of zero."""
    body = client.get(UPLOAD_URL).json()

    assert body["total"] == 0
    assert body["results"] == []


def test_list_returns_total_and_results(client: TestClient) -> None:
    """Uploaded images appear in the list with a matching total."""
    for _ in range(3):
        upload(client, make_image_bytes(100, 100), presets="small")

    body = client.get(UPLOAD_URL).json()

    assert body["total"] == 3
    assert len(body["results"]) == 3


def test_filter_by_status(client: TestClient, sample_image: bytes) -> None:
    """Filtering by status narrows the result set."""
    upload(client, sample_image, presets="small")

    ready = client.get(UPLOAD_URL, params={"status": "ready"}).json()
    failed = client.get(UPLOAD_URL, params={"status": "failed"}).json()

    assert ready["total"] == 1
    assert failed["total"] == 0


def test_filter_by_content_type(client: TestClient, sample_image: bytes) -> None:
    """Filtering by content type narrows the result set."""
    upload(client, sample_image, presets="small")

    jpeg = client.get(UPLOAD_URL, params={"content_type": "image/jpeg"}).json()
    png = client.get(UPLOAD_URL, params={"content_type": "image/png"}).json()

    assert jpeg["total"] == 1
    assert png["total"] == 0


def test_invalid_status_filter_rejected(client: TestClient) -> None:
    """An unknown status filter value is rejected."""
    assert client.get(UPLOAD_URL, params={"status": "bogus"}).status_code == 422


def test_pagination_limits_results_but_not_total(client: TestClient) -> None:
    """Limit caps the page size while total reflects every match."""
    for _ in range(5):
        upload(client, make_image_bytes(100, 100), presets="small")

    body = client.get(UPLOAD_URL, params={"limit": 2}).json()

    assert body["total"] == 5
    assert len(body["results"]) == 2
    assert body["limit"] == 2


def test_pagination_offset_moves_the_window(client: TestClient) -> None:
    """Offset skips earlier results without changing the total."""
    for _ in range(4):
        upload(client, make_image_bytes(100, 100), presets="small")

    body = client.get(UPLOAD_URL, params={"limit": 2, "offset": 3}).json()

    assert body["total"] == 4
    assert len(body["results"]) == 1
    assert body["offset"] == 3


def test_limit_above_maximum_rejected(client: TestClient) -> None:
    """A limit above the allowed maximum is rejected."""
    assert client.get(UPLOAD_URL, params={"limit": 500}).status_code == 422


# --------------------------------------------------------------------------
# Concurrency and customer-facing extras
# --------------------------------------------------------------------------


def test_concurrent_requests_are_served(client: TestClient, sample_image: bytes) -> None:
    """The service handles simultaneous read requests without errors."""
    upload(client, sample_image, presets="small")

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.get(UPLOAD_URL), range(16)))

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["total"] == 1 for response in responses)


def test_list_presets(client: TestClient) -> None:
    """The presets endpoint documents every supported preset."""
    body = client.get("/v1/presets").json()

    names = {preset["name"] for preset in body["presets"]}
    assert names == {"small", "medium", "large", "custom"}


def test_stats_reflect_uploads(client: TestClient, sample_image: bytes) -> None:
    """Usage statistics count stored images and thumbnails."""
    upload(client, sample_image, presets="small,medium")

    body = client.get("/v1/stats").json()

    assert body["images"] == 1
    assert body["thumbnails"] == 2
    assert body["thumbnail_bytes"] > 0
