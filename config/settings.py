import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "development-only-fuel-planner")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")

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
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "APP_DIRS": True}]

# No models: the app only reads the bundled CSV files.
DATABASES = {}

TESTING = sys.argv[1:2] == ["test"]

# "routes" is the persistent second-level route cache (planner/cache.py keeps a first level in memory).
# File-based: shared by all workers on a host and kept across restarts. Use Redis for several hosts.
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "routes": (
        {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "test-routes"}
        if TESTING
        else {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": os.environ.get("ROUTE_CACHE_DIR", str(BASE_DIR / ".cache" / "routes")),
            "OPTIONS": {"MAX_ENTRIES": 10000},
        }
    ),
}
ROUTE_CACHE_SECONDS = int(os.environ.get("ROUTE_CACHE_SECONDS", 7 * 24 * 3600))

# Free, keyless OSRM servers: the primary, then a compatible mirror used only if the primary
# fails. Sequential attempts preserve the repair allowance.
OSRM_URLS = [
    url.strip().rstrip("/")
    for url in os.environ.get(
        "OSRM_URLS", "https://router.project-osrm.org,https://routing.openstreetmap.de/routed-car"
    ).split(",")
    if url.strip()
]
OSRM_CONNECT_TIMEOUT = 3.05
OSRM_READ_TIMEOUT = 15
# Street addresses only (cities and coordinates resolve offline).
GEOCODER_URL = os.environ.get("GEOCODER_URL", "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress")

# Optional connection warming adds HTTP traffic; disabled for the public demo servers.
OSRM_KEEP_WARM_SECONDS = int(os.environ.get("OSRM_KEEP_WARM_SECONDS", 0))
OSRM_KEEPALIVE_INTERVAL = 45

SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"  # OSM tiles require a browser Referer.

DATA_UPLOAD_MAX_MEMORY_SIZE = 16384
USE_TZ = True
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "planner": {"handlers": ["console"], "level": "INFO"},
        "django.request": {"handlers": ["console"], "level": "ERROR"},  # Tracebacks of any 500.
    },
}
