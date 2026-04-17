"""Tests for subagent runtime configuration.

Covers:
- SubagentsAppConfig / SubagentOverrideConfig model validation and defaults
- get_timeout_for() / get_max_turns_for() resolution logic
- AppConfig.subagents field access
- registry.get_subagent_config() applies config overrides
- registry.list_subagents() applies overrides for all agents
- Polling timeout calculation in task_tool is consistent with config
"""

import pytest

from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.subagents_config import (
    SubagentOverrideConfig,
    SubagentsAppConfig,
)


def _make_config(
    timeout_seconds: int = 900,
    *,
    max_turns: int | None = None,
    agents: dict | None = None,
) -> AppConfig:
    """Build an AppConfig with the given subagents settings."""
    return AppConfig(
        sandbox=SandboxConfig(use="test"),
        subagents=SubagentsAppConfig(
            timeout_seconds=timeout_seconds,
            max_turns=max_turns,
            agents={k: SubagentOverrideConfig(**v) for k, v in (agents or {}).items()},
        ),
    )


# ---------------------------------------------------------------------------
# SubagentOverrideConfig
# ---------------------------------------------------------------------------


class TestSubagentOverrideConfig:
    def test_default_is_none(self):
        override = SubagentOverrideConfig()
        assert override.timeout_seconds is None
        assert override.max_turns is None

    def test_explicit_values(self):
        override = SubagentOverrideConfig(timeout_seconds=120, max_turns=50)
        assert override.timeout_seconds == 120
        assert override.max_turns == 50

    def test_rejects_negative_timeout(self):
        with pytest.raises(Exception):
            SubagentOverrideConfig(timeout_seconds=-1)

    def test_rejects_zero_timeout(self):
        with pytest.raises(Exception):
            SubagentOverrideConfig(timeout_seconds=0)


# ---------------------------------------------------------------------------
# SubagentsAppConfig model
# ---------------------------------------------------------------------------


class TestSubagentsAppConfig:
    def test_default_timeout_is_900(self):
        config = SubagentsAppConfig()
        assert config.timeout_seconds == 900
        assert config.max_turns is None
        assert config.agents == {}

    def test_custom_defaults(self):
        config = SubagentsAppConfig(timeout_seconds=300, max_turns=50)
        assert config.timeout_seconds == 300
        assert config.max_turns == 50


# ---------------------------------------------------------------------------
# get_timeout_for / get_max_turns_for
# ---------------------------------------------------------------------------


class TestTimeoutResolution:
    def test_global_timeout_for_unknown_agent(self):
        config = SubagentsAppConfig(timeout_seconds=600)
        assert config.get_timeout_for("unknown") == 600

    def test_per_agent_timeout_overrides_global(self):
        config = SubagentsAppConfig(
            timeout_seconds=600,
            agents={"bash": SubagentOverrideConfig(timeout_seconds=120)},
        )
        assert config.get_timeout_for("bash") == 120
        assert config.get_timeout_for("general-purpose") == 600

    def test_per_agent_override_none_falls_back_to_global(self):
        config = SubagentsAppConfig(
            timeout_seconds=600,
            agents={"bash": SubagentOverrideConfig(timeout_seconds=None)},
        )
        assert config.get_timeout_for("bash") == 600


class TestMaxTurnsResolution:
    def test_builtin_default_when_no_override(self):
        config = SubagentsAppConfig()
        assert config.get_max_turns_for("bash", 60) == 60

    def test_global_max_turns_overrides_builtin(self):
        config = SubagentsAppConfig(max_turns=100)
        assert config.get_max_turns_for("bash", 60) == 100

    def test_per_agent_max_turns_overrides_global(self):
        config = SubagentsAppConfig(
            max_turns=100,
            agents={"bash": SubagentOverrideConfig(max_turns=30)},
        )
        assert config.get_max_turns_for("bash", 60) == 30
        assert config.get_max_turns_for("general-purpose", 60) == 100

    def test_per_agent_override_none_falls_back(self):
        config = SubagentsAppConfig(
            max_turns=100,
            agents={"bash": SubagentOverrideConfig(max_turns=None)},
        )
        assert config.get_max_turns_for("bash", 60) == 100


# ---------------------------------------------------------------------------
# AppConfig.subagents
# ---------------------------------------------------------------------------


class TestAppConfigSubagents:
    def test_load_global_timeout(self):
        cfg = _make_config(timeout_seconds=300, max_turns=120)
        sub = cfg.subagents
        assert sub.timeout_seconds == 300
        assert sub.max_turns == 120

    def test_load_with_per_agent_overrides(self):
        cfg = _make_config(
            timeout_seconds=900,
            max_turns=120,
            agents={
                "general-purpose": {"timeout_seconds": 1800, "max_turns": 200},
                "bash": {"timeout_seconds": 60, "max_turns": 80},
            },
        )
        sub = cfg.subagents
        assert sub.get_timeout_for("general-purpose") == 1800
        assert sub.get_timeout_for("bash") == 60
        assert sub.get_max_turns_for("general-purpose", 100) == 200
        assert sub.get_max_turns_for("bash", 60) == 80

    def test_load_partial_override(self):
        cfg = _make_config(
            timeout_seconds=600,
            agents={"bash": {"timeout_seconds": 120, "max_turns": 70}},
        )
        sub = cfg.subagents
        assert sub.get_timeout_for("general-purpose") == 600
        assert sub.get_timeout_for("bash") == 120
        assert sub.get_max_turns_for("general-purpose", 100) == 100
        assert sub.get_max_turns_for("bash", 60) == 70

    def test_load_empty_uses_defaults(self):
        cfg = _make_config()
        sub = cfg.subagents
        assert sub.timeout_seconds == 900
        assert sub.max_turns is None
        assert sub.agents == {}
