"""WSGI config for the agri-marketplace project.

Exposes the WSGI callable as a module-level variable named ``application``.
Used by production servers such as gunicorn: ``gunicorn config.wsgi:application``.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
