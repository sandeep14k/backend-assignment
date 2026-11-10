import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "development-only-fuel-planner")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
).split(",")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
INSTALLED_APPS = ["planner"]
MIDDLEWARE = [
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [
    {"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}
]

# No models: the app only reads the bundled CSV files.
DATABASES = {}

# Route cache. Swap for Redis (django.core.cache.backends.redis.RedisCache) to share across workers.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "routes",
        "TIMEOUT": 86400,
        "OPTIONS": {"MAX_ENTRIES": 256},
    }
}

# Free, keyless OSRM servers: the primary, then an independent mirror used only if the primary fails.
OSRM_URLS = [
    url.strip()
    for url in os.environ.get(
        "OSRM_URLS",
        "https://router.project-osrm.org,https://routing.openstreetmap.de/routed-car",
    ).split(",")
    if url.strip()
]

DATA_UPLOAD_MAX_MEMORY_SIZE = 16384
USE_TZ = True
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"planner": {"handlers": ["console"], "level": "INFO"}},
}
