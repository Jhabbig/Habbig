"""Hardened-session authentication module for narve.ai.

Public surface (everything else lives in submodules):

    from auth import (
        SESSION_COOKIE,
        set_session_cookie_hardened,
        clear_session_cookie_hardened,
        read_hardened_session,
        attach_session_to_request,
        require_hardened_session,
        require_hardened_admin,
    )

The old ``sessions`` table + ``current_user`` helper in server.py keep
working; this module layers a hardened session gate on top for routes
that care (``/register``, ``/login``, ``/auth/*``, ``/api/auth/sessions``).
"""

from auth.cookies import (  # noqa: F401
    SESSION_COOKIE,
    set_session_cookie_hardened,
    clear_session_cookie_hardened,
)
from auth.guards import (  # noqa: F401
    read_hardened_session,
    attach_session_to_request,
    require_hardened_session,
    require_hardened_admin,
    # Spec-exact aliases
    require_auth,
    require_admin,
)
