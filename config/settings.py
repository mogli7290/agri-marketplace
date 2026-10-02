"""
Django settings for the agri-marketplace project.

Configuration is environment-driven so the same image can run in development,
staging and production. Copy ``.env.example`` to ``.env`` for local use; in
production inject real environment variables through your host.

Reference: https://docs.djangoproject.com/en/5.2/ref/settings/
"""

import os
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no third-party dependency).

    Existing environment variables always win, so production values are never
    overwritten by a stray local file.
    """
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return str(os.environ.get(key, default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(key: str, default: str = "") -> list[str]:
    raw = os.environ.get(key, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "django-insecure-dev-only-key-do-not-use-in-production"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,10.208.181.156"
)

# Health checks and platform probes frequently use the machine hostname.
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Managed platforms (Render, etc.) expose the public hostname at runtime, which
# may gain a suffix if the requested name is taken. Trust it automatically.
_platform_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _platform_host:
    if _platform_host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_platform_host)
    for scheme in ("https", "http"):
        origin = f"{scheme}://{_platform_host}"
        if origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(origin)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "corsheaders",
    # Local
    "marketplace",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "marketplace.context_processors.site_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Accepts a single DATABASE_URL (Neon, Railway, Render, etc.). Falls back to a
# local SQLite file for convenience during development.
DATABASE_URL = env("DATABASE_URL")


def _database_from_url(url: str) -> dict:
    parsed = urlparse(url)
    scheme_map = {
        "postgres": "django.db.backends.postgresql",
        "postgresql": "django.db.backends.postgresql",
        "postgis": "django.contrib.gis.db.backends.postgis",
    }
    engine = scheme_map.get(parsed.scheme, "django.db.backends.postgresql")
    config = {
        "ENGINE": engine,
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username or "",
        "PASSWORD": parsed.password or "",
        "HOST": parsed.hostname or "",
        "PORT": str(parsed.port or ""),
        "CONN_MAX_AGE": int(env("DB_CONN_MAX_AGE", "600")),
        "OPTIONS": {},
    }
    # Neon and most managed providers require TLS.
    if env_bool("DB_REQUIRE_SSL", True) and config["HOST"] not in ("", "localhost", "127.0.0.1"):
        config["OPTIONS"]["sslmode"] = "require"
    return config


if DATABASE_URL:
    DATABASES = {"default": _database_from_url(DATABASE_URL)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "marketplace.auth_backends.EmailOrUsernameBackend",
    "django.contrib.auth.backends.ModelBackend",
]

LOGIN_URL = "marketplace:login"
LOGIN_REDIRECT_URL = "marketplace:dashboard"
LOGOUT_REDIRECT_URL = "marketplace:login"

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-in"
TIME_ZONE = env("DJANGO_TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedStaticFilesStorage"
        )
    },
}

# Optional S3-compatible object storage for user uploads (django-storages).
if env_bool("USE_S3", False):
    AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
    AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
    AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME")
    AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL") or None
    AWS_S3_CUSTOM_DOMAIN = env("AWS_S3_CUSTOM_DOMAIN") or None
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = env_bool("AWS_QUERYSTRING_AUTH", False)
    bucket = AWS_STORAGE_BUCKET_NAME or ""
    if AWS_S3_CUSTOM_DOMAIN:
        public_url = f"https://{AWS_S3_CUSTOM_DOMAIN}"
    elif AWS_S3_ENDPOINT_URL:
        public_url = f"{AWS_S3_ENDPOINT_URL.rstrip('/')}/{bucket}"
    else:
        public_url = f"https://{bucket}.s3.amazonaws.com"
    AWS_S3_OBJECT_PARAMETERS = {"CacheControl": "max-age=86400"}
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage"}
    MEDIA_URL = f"{public_url.rstrip('/')}/"

# ---------------------------------------------------------------------------
# Cache (Redis in production, in-process memory locally)
# ---------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            "KEY_PREFIX": "agrimarket",
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "agrimarket-local",
        }
    }

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend" if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "AgriMarket <no-reply@agrimarket.local>")

# Brevo (transactional email). The API key is used by
# ``marketplace.email_backend.BrevoEmailBackend``; Brevo issues API and SMTP
# keys separately and they are NOT interchangeable.
BREVO_API_KEY = env("BREVO_API_KEY", "")
BREVO_API_BASE_URL = env("BREVO_API_BASE_URL", "https://api.brevo.com")

# Public base URL, used to build absolute links in emails (verification, etc.)
# when there is no request to derive it from.
SITE_URL = (env("SITE_URL", "") or "").rstrip("/")

# On Render the public hostname is known at boot and may carry a suffix if the
# name you asked for was taken. Deriving SITE_URL from it means every email link
# is absolute and correct on the first deploy, with nothing to remember and no
# chance of pasting the wrong one. An explicit SITE_URL still wins.
if not SITE_URL and _platform_host:
    SITE_URL = f"https://{_platform_host}"

# --- Email verification ----------------------------------------------------
# New accounts must prove they own the address they registered with before they
# can log in. Accounts created out-of-band (admin, seed data, createsuperuser)
# have no EmailVerification row and are treated as already verified.
EMAIL_VERIFICATION_REQUIRED = env_bool("EMAIL_VERIFICATION_REQUIRED", True)
EMAIL_VERIFICATION_MAX_AGE = int(env("EMAIL_VERIFICATION_MAX_AGE_HOURS", "48")) * 3600
# Throttle re-sending: a link is only re-emitted this many seconds after the last one.
EMAIL_VERIFICATION_RESEND_COOLDOWN = int(env("EMAIL_VERIFICATION_RESEND_COOLDOWN", "60"))

# How long a password-reset link stays valid. Short on purpose: the link is a
# way in, and a farmer on a shared phone should not leave one lying around.
PASSWORD_RESET_TIMEOUT = int(env("PASSWORD_RESET_TIMEOUT_HOURS", "24")) * 3600

# --- Transactional notifications --------------------------------------------
# Order/offer/payment/delivery/payout mail. Set to 0 to silence everything
# without touching any call site — useful while a mail provider is misbehaving.
NOTIFICATIONS_ENABLED = env_bool("NOTIFICATIONS_ENABLED", True)

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "marketplace.api.pagination.StandardResultsPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": env("THROTTLE_ANON", "60/min"),
        "user": env("THROTTLE_USER", "1000/day"),
        "ai": env("THROTTLE_AI", "30/hour"),
    },
    "DEFAULT_SCHEMA_CLASS": "rest_framework.schemas.openapi.AutoSchema",
}

from datetime import timedelta  # noqa: E402  (kept near its only consumer)

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(env("JWT_ACCESS_MINUTES", "30"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(env("JWT_REFRESH_DAYS", "7"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS")
CORS_ALLOW_ALL_ORIGINS = DEBUG and not CORS_ALLOWED_ORIGINS

# ---------------------------------------------------------------------------
# Third-party integrations
# ---------------------------------------------------------------------------
# Razorpay (UPI / cards / netbanking). Leave blank to disable online payments.
RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = env("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_CURRENCY = env("RAZORPAY_CURRENCY", "INR")

# Direct UPI — pay straight to a UPI ID (VPA), no gateway or keys needed.
# Leave UPI_ID blank to hide the "Pay via UPI" option entirely.
UPI_ID = env("UPI_ID", "")
UPI_PAYEE_NAME = env("UPI_PAYEE_NAME", "")

# AI provider (OpenAI-compatible). SambaNova Cloud is the default:
#   base_url = https://api.sambanova.ai/v1
AI_API_KEY = env("AI_API_KEY", "")
AI_BASE_URL = env("AI_BASE_URL", "https://api.sambanova.ai/v1")
AI_MODEL = env("AI_MODEL", "Meta-Llama-3.1-8B-Instruct")
AI_TIMEOUT_SECONDS = int(env("AI_TIMEOUT_SECONDS", "20"))

# Marketplace economics.
PLATFORM_FEE_PERCENT = env("PLATFORM_FEE_PERCENT", "2.0")
DELIVERY_BASE_FEE = env("DELIVERY_BASE_FEE", "25.0")
DELIVERY_PER_KM_FEE = env("DELIVERY_PER_KM_FEE", "6.0")

# Days a settled order waits before it can be paid out. The hold leaves room
# for a return, a quality claim or a refund to be taken back first.
PAYOUT_HOLD_DAYS = int(env("PAYOUT_HOLD_DAYS", "7"))

SITE_NAME = env("SITE_NAME", "AgriMarket")

# Map Django's message levels onto Bootstrap alert classes.
from django.contrib.messages import constants as message_constants  # noqa: E402

MESSAGE_TAGS = {
    message_constants.DEBUG: "secondary",
    message_constants.INFO: "info",
    message_constants.SUCCESS: "success",
    message_constants.WARNING: "warning",
    message_constants.ERROR: "danger",
}

# ---------------------------------------------------------------------------
# Security hardening (enabled outside DEBUG)
# ---------------------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # must be readable for JS clients posting via form
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

if not DEBUG:
    # Always enforce transport security in production; a stray local .env must
    # never be able to turn these off.
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "marketplace": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}
