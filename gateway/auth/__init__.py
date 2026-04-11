"""Token-first authentication module for narve.ai.

Public surface (everything else lives in submodules):

    from auth import (
        PENDING_TOKEN_COOKIE,
        SESSION_COOKIE,
        set_pending_token_cookie,
        clear_pending_token_cookie,
        read_pending_token,
        set_session_cookie_hardened,
        clear_session_cookie_hardened,
        read_hardened_session,
        attach_session_to_request,
        require_pending_token,
        require_hardened_session,
        require_hardened_admin,
    )

The old `sessions` table + `current_user` helper in server.py keep
working; this module layers a new token-first gate on top for routes
that care (`/register`, `/login`, `/auth/*`, `/api/auth/sessions`).
"""

from auth.cookies import (  # noqa: F401
    PENDING_TOKEN_COOKIE,
    SESSION_COOKIE,
    PENDING_TOKEN_TTL,
    set_pending_token_cookie,
    clear_pending_token_cookie,
    read_pending_token,
    sign_pending_token,
    verify_pending_token,
    set_session_cookie_hardened,
    clear_session_cookie_hardened,
)
from auth.guards import (  # noqa: F401
    read_hardened_session,
    attach_session_to_request,
    require_pending_token,
    require_hardened_session,
    require_hardened_admin,
    # Spec-exact aliases
    require_auth,
    require_admin,
)
