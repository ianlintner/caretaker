"""OAuth2 client helpers for caretaker service-to-service auth.

Separate from :mod:`caretaker.admin.auth`, which handles the interactive
OIDC login flow for the admin dashboard. This package contains the
service-side client used by caretaker when it calls out to OAuth2-protected
backends (fleet registry, remote MCP servers, etc.) from CI runs.
"""

from caretaker.auth.oauth_client import (
    OAuth2ClientCredentials,
    OAuth2TokenError,
    build_client_from_env,
)

__all__ = [
    "OAuth2ClientCredentials",
    "OAuth2TokenError",
    "build_client_from_env",
]
