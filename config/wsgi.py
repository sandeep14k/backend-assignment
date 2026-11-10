import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()

# Load city and station data at startup so no request pays for it.
from planner.data import warm  # noqa: E402

warm()
