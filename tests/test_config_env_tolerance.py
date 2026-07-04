# -*- coding: utf-8 -*-
"""Settings 对未知 env 键的容忍策略回归。

2026-07-04 19:00 生产实证:.env 先于代码部署新增 KF_MEDIA_LINK_SECRET,
pydantic-settings 默认 extra="forbid" 使所有消费 Settings 的进程构造即抛
extra_forbidden。口径改为 extra="ignore":配置先行不再致命,未知键静默忽略。
"""
from __future__ import annotations

from app.config import Settings


def test_settings_ignores_unknown_env_keys(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    # 模拟"配置先行于代码部署"的运维顺序:代码尚无该字段。
    monkeypatch.setenv("KF_FUTURE_FEATURE_SECRET_NOT_DECLARED", "placeholder")

    settings = Settings()

    assert settings.app_env == "test"
    assert not hasattr(settings, "kf_future_feature_secret_not_declared")


def test_settings_still_parses_declared_keys(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings()

    assert settings.log_level == "DEBUG"
