import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()

# Startup work so no request pays for it: load datasets, build the station
# KD-tree, and keep HTTPS connections to the routing servers warm.
import planner.services  # noqa: E402,F401  Loads the optimizer and SciPy now, not on the first request.
from planner.data import warm  # noqa: E402
from planner.routing import start_connection_warmer  # noqa: E402

warm()
start_connection_warmer()
