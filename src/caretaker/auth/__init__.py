"""OAuth2 client and bearer-token helpers for caretaker service-to-service auth.

Separate from :mod:`caretaker.admin.auth`, which handles the interactive
OIDC login flow for the admin dashboard. This package contains:

* :mod:`caretaker.auth.oauth_client` — outbound OAuth2 client_credentials
  client used when caretaker calls remote OAuth2-protected endpoints from CI.
* :mod:`caretaker.auth.bearer` — inbound JWT bearer-token verifier used by
  caretaker backend resources (fleet heartbeat, MCP, future endpoints).

Both share a single OAuth2 issuer for the deployment, providing one unified
auth path across MCP, heartbeat, and any other authenticated resource.
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
