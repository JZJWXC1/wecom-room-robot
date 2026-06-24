from __future__ import annotations

import pytest


@pytest.mark.online
def test_online_marker_gate_requires_explicit_env() -> None:
    assert True
