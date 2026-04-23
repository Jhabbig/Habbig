"""End-to-end flows — one file per user journey.

Each test simulates a full journey (gate → register → login → …) and
asserts the invariants each step is supposed to preserve. Tests that
reference a feature not shipped in this tree skip explicitly so the
suite is always green on the current main.
"""
