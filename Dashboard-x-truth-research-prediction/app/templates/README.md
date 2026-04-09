# app/templates/ — Jinja2 templates

HTML templates served by `app/main.py` via FastAPI's `Jinja2Templates`. The
dashboard intentionally uses server-side rendering with no JS framework — just
plain HTML, a sprinkle of CSS, and small `<script>` blocks where needed.

## Files in this directory

| File | Purpose |
|---|---|
| `dashboard.html` | The main authenticated view. Lists predictions with EV, source credibility, market links, and risk flags. Manual "Refresh" button hits `/refresh`. |
| `login.html` | Login form. POSTs to `/login`, sets the Fernet-encrypted session cookie. |
| `register.html` | Signup form. Disabled in production unless self-serve registration is enabled in `config.yaml`. |
| `forgot_password.html` | Password-reset request form. Sends a reset token if SMTP is configured. |
| `profile.html` | Authenticated user's profile page — change password, view API quota. |
