"""Intelligence assistant module — context builder + Claude streaming."""
from intelligence.context import build_intelligence_context  # noqa: F401
from intelligence.claude_client import (  # noqa: F401
    INTELLIGENCE_SYSTEM_PROMPT,
    stream_intelligence_response,
    get_intelligence_response,
)
