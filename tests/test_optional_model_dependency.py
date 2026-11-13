"""An optional native dependency must not abort the usable scorecard baseline."""

import builtins

from credit_agent.modeling.workflows import training


def test_lightgbm_native_loader_failure_is_optional(monkeypatch):
    original = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "lightgbm":
            raise OSError("missing native OpenMP library")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    assert training._new_lgbm(0) is None
