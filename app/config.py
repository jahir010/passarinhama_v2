from typing import Optional
from pydantic_settings import BaseSettings
from tortoise import Tortoise
from app.utils.auto_routing import get_apps_structure


class Settings(BaseSettings):
    DEBUG: bool = True
    APP_NAME: str = "FastAPI App"
    MEDIA_DIR: str = "media/"
    MEDIA_ROOT: str = "media/"
    ENV: str = "development"
    CREATE_DUMMY_DATA: bool = True

    EMAIL_HOST: str = "smtp.gmail.com"
    EMAIL_PORT: int = 587
    EMAIL_HOST_USER: str = "lookuparchitecture@gmail.com"
    EMAIL_HOST_PASSWORD: str = "ihuciytlvdymggol"
    DEFAULT_FROM_EMAIL: str = "lookuparchitecture@gmail.com"

    DB_HOST: str = "localhost"
    DB_NAME: str = "db.sqlite3"
    DB_USER: str = ""
    DB_PASSWORD: str = ""
    DB_ROOT_PASSWORD: str = ""
    DB_PORT: int = 5432
    DB_ENGINE: str = "postgres"

    DATABASE_URL: Optional[str] = None

    SECRET_KEY: Optional[str] = None
    BASE_URL: str = "http://localhost:8000/"
    RADIS_URL: str = "redis://localhost:6379/0"

    def model_post_init(self, __context):
        if self.DB_ENGINE == "sqlite":
            self.DATABASE_URL = f"sqlite:///{self.DB_NAME}"
        else:
            self.DATABASE_URL = (
                f"{self.DB_ENGINE}://{self.DB_USER}:{self.DB_PASSWORD}"
                f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

TORTOISE_ORM = {
    "connections": {
        "default": settings.DATABASE_URL,
    },
    "apps": get_apps_structure("applications"),
    "use_tz": True,
    "timezone": "UTC",
}

async def init_db():
    if getattr(Tortoise, "_inited", False):
        return
    await Tortoise.init(config=TORTOISE_ORM)
    if settings.ENV != "production":
        await Tortoise.generate_schemas()
    else:
        print("Skipping schema generation in production.")


async def close_db():
    if not getattr(Tortoise, "_inited", False):
        return
    await Tortoise.close_connections()



