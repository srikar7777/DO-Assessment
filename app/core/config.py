"""Application settings loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration for the thumbnail service (12-factor)."""

    model_config = ConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="myservice", min_length=1, max_length=100)
    app_version: str = Field(default="0.1.0", min_length=1, max_length=32)
    app_port: int = Field(default=8000, ge=1, le=65535)
    environment: Literal["development", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: str = Field(
        default="sqlite:///./myservice.db",
        min_length=1,
        max_length=2048,
    )

    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: str = Field(default="./storage", min_length=1, max_length=512)

    s3_endpoint_url: str = Field(default="", max_length=512)
    s3_region: str = Field(default="nyc3", max_length=64)
    s3_bucket: str = Field(default="", max_length=255)
    s3_access_key_id: str = Field(default="", max_length=255)
    s3_secret_access_key: str = Field(default="", max_length=255)
    s3_public_base_url: str = Field(default="", max_length=512)

    cors_origins: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        min_length=1,
        max_length=2048,
    )
    api_key: str = Field(default="", max_length=255)
    rate_limit_requests_per_minute: int = Field(default=60, ge=0, le=100000)

    max_upload_bytes: int = Field(default=10_485_760, ge=1024, le=104_857_600)
    max_images_per_request: int = Field(default=10, ge=1, le=50)
    default_thumbnail_quality: int = Field(default=85, ge=1, le=100)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: str) -> str:
        """Reject blank or whitespace-only database URLs."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("DATABASE_URL must not be blank")
        return cleaned

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: str) -> str:
        """Reject blank CORS origin lists."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("CORS_ORIGINS must not be blank")
        return cleaned

    @field_validator("storage_dir")
    @classmethod
    def validate_storage_dir(cls, value: str) -> str:
        """Reject blank local storage directories."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("STORAGE_DIR must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_production_guards(self) -> "Settings":
        """Enforce safer defaults when deploying to production (e.g. DigitalOcean)."""
        if self.environment == "production":
            if not self.api_key.strip():
                raise ValueError("API_KEY is required when ENVIRONMENT=production")
            if self.cors_origins.strip() == "*":
                raise ValueError("CORS_ORIGINS='*' is not allowed in production")
            if self.storage_backend == "s3":
                required = {
                    "S3_ENDPOINT_URL": self.s3_endpoint_url,
                    "S3_BUCKET": self.s3_bucket,
                    "S3_ACCESS_KEY_ID": self.s3_access_key_id,
                    "S3_SECRET_ACCESS_KEY": self.s3_secret_access_key,
                }
                missing = [name for name, val in required.items() if not val.strip()]
                if missing:
                    raise ValueError(
                        "S3 storage requires these env vars in production: "
                        + ", ".join(missing)
                    )
            if self.database_url.startswith("sqlite"):
                raise ValueError(
                    "SQLite is not allowed when ENVIRONMENT=production; "
                    "use a managed PostgreSQL DATABASE_URL"
                )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def database_log_label(self) -> str:
        """Safe DB label for logs (scheme/driver only, never full URL)."""
        return self.database_url.split("///")[0]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
