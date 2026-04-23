"""Mock external services for deterministic tests.

Every submodule exports a callable fixture-builder that tests can import
directly (``from tests.mocks.anthropic import mock_anthropic``) or a
plain class tests can instantiate themselves for parameterised flows.

The goal is one well-known set of fakes so every integration test talks
to the same shape — instead of each file rolling its own AsyncMock
chain.
"""
