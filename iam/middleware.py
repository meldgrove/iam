"""Response headers for a private admin app."""

from urllib.parse import urlsplit

from django.conf import settings


def _provider_origin() -> str:
    parts = urlsplit(settings.OIDC_ISSUER or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        # Sign-out posts a form that redirects to the provider, so the
        # provider's origin must be an allowed form target.
        form_targets = " ".join(filter(None, ["'self'", _provider_origin()]))
        self.csp = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"form-action {form_targets}; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
        )

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", self.csp)
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.user.is_authenticated or request.path.startswith("/-/"):
            response.headers["Cache-Control"] = "no-store"
        return response
