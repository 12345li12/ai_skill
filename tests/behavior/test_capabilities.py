"""CapabilityProbe / CapabilityRegistry 行为测试（第 3 步 3.2）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-CAP-001  检查并缓存 8 项能力                        → TestSnapshotShape
  TEST-CAP-002  6 个重查触发条件，只重查变化部分             → TestEventDrivenRecheck
  TEST-CAP-003  无真实子代理 → 如实说明 + 自动单代理阶段化    → TestNoRealSubagent
  TEST-CAP-004  不能热切换 → 停旧分支、复用结果、按新配置重建 → TestHotSwitch
  TEST-CAP-005  只使用环境真实暴露的能力；禁止假分流          → TestNoFabrication
"""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_router.capabilities import (
    FIXED_DIMENSIONS,
    CapabilityDimension,
    CapabilityProbe,
    CapabilityRegistry,
    EnvironmentReading,
    ProbeOutcome,
    RecheckTrigger,
)
from adaptive_router.contracts import EffortLevel, UnavailableReason


def reading(**overrides: object) -> EnvironmentReading:
    """默认：8 项能力全部可确认的理想环境。"""
    base: dict[str, object] = dict(
        subagents=True,
        async_background=True,
        concurrency_limit=4,
        available_models=("gpt-5-codex", "gpt-5"),
        available_effort_levels=(EffortLevel.LOW, EffortLevel.HIGH),
        state_wait_append_stop=True,
        hot_switch=False,
        reliable_cost_data=False,
    )
    base.update(overrides)
    return EnvironmentReading(**base)  # type: ignore[arg-type]


def probe() -> CapabilityProbe:
    return CapabilityProbe(probed_at="2026-09-16T00:00:00Z")


# --------------------------------------------------------------------------
# TEST-CAP-001
# --------------------------------------------------------------------------


class TestSnapshotShape:
    def test_covers_all_eight_capability_dimensions(self) -> None:
        """L13 的 8 项能力一个不少。"""
        assert len(CapabilityDimension) == 8
        snapshot = probe().snapshot(reading())
        assert set(snapshot.probed_dimensions) == {d.value for d in CapabilityDimension}

    def test_snapshot_is_a_frozen_contract_type(self) -> None:
        from adaptive_router.contracts import CapabilitySnapshot

        snapshot = probe().snapshot(reading())
        assert isinstance(snapshot, CapabilitySnapshot)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.subagents = False  # type: ignore[misc]

    def test_probed_at_comes_from_the_probe(self) -> None:
        snapshot = CapabilityProbe(probed_at="2026-01-01T00:00:00Z").snapshot(reading())
        assert snapshot.probed_at == "2026-01-01T00:00:00Z"

    def test_all_confirmable_reading_has_no_unknown(self) -> None:
        snapshot = probe().snapshot(reading())
        assert snapshot.is_unknown is False
        assert snapshot.unavailable_reason is None


# --------------------------------------------------------------------------
# TEST-CAP-002 —— 事件驱动，只重查可能变化的维度
# --------------------------------------------------------------------------


class TestEventDrivenRecheck:
    def test_six_triggers_are_defined(self) -> None:
        """L15 的重查触发条件。"""
        assert {t.name for t in RecheckTrigger} == {
            "TOOL_FAILURE",
            "MANIFEST_CHANGED",
            "REQUEST_REJECTED",
            "CONCURRENCY_ANOMALY",
            "ENVIRONMENT_SWITCHED",
            "SESSION_RESUMED",
        }

    def test_no_timer_based_recheck(self) -> None:
        """只重查可能变化的维度，**不定时轮询**。"""
        field_names = {f.name for f in dataclasses.fields(CapabilityRegistry)}
        for forbidden in ("interval", "poll_seconds", "ttl", "next_probe_at", "cron"):
            assert forbidden not in field_names

    def test_registry_reuses_cached_snapshot_without_a_trigger(self) -> None:
        """没有触发条件时不得重新探测。"""
        calls: list[object] = []

        def loader() -> EnvironmentReading:
            calls.append(1)
            return reading()

        registry = CapabilityRegistry(probe=probe(), loader=loader)
        first = registry.snapshot()
        second = registry.snapshot()
        assert first is second
        assert len(calls) == 1  # 只探测了一次

    def test_trigger_causes_a_reprobe(self) -> None:
        calls: list[object] = []

        def loader() -> EnvironmentReading:
            calls.append(1)
            return reading()

        registry = CapabilityRegistry(probe=probe(), loader=loader)
        before = registry.snapshot()
        after = registry.recheck(RecheckTrigger.TOOL_FAILURE)
        assert len(calls) == 2
        assert after is not before
        assert before.subagents is True

    def test_only_changed_dimensions_are_reprobed(self) -> None:
        """L15：只重查**变化部分**。"""
        requested: list[set[CapabilityDimension]] = []

        def loader(dimensions: set[CapabilityDimension]) -> EnvironmentReading:
            requested.append(set(dimensions))
            return reading()

        registry = CapabilityRegistry(probe=probe(), loader=loader)
        registry.snapshot()
        registry.recheck(RecheckTrigger.TOOL_FAILURE)
        assert requested[0] == set(CapabilityDimension)  # 首次全量
        assert requested[1] == {
            CapabilityDimension.SUBAGENTS,
            CapabilityDimension.STATE_WAIT_APPEND_STOP,
            CapabilityDimension.HOT_SWITCH,
        }

    def test_manifest_change_targets_models_and_effort(self) -> None:
        requested: list[set[CapabilityDimension]] = []

        def loader(dimensions: set[CapabilityDimension]) -> EnvironmentReading:
            requested.append(set(dimensions))
            return reading()

        registry = CapabilityRegistry(probe=probe(), loader=loader)
        registry.snapshot()
        registry.recheck(RecheckTrigger.MANIFEST_CHANGED)
        assert requested[1] == {
            CapabilityDimension.AVAILABLE_MODELS,
            CapabilityDimension.AVAILABLE_EFFORT_LEVELS,
        }

    def test_environment_switch_targets_the_fixed_dimensions(self) -> None:
        requested: list[set[CapabilityDimension]] = []

        def loader(dimensions: set[CapabilityDimension]) -> EnvironmentReading:
            requested.append(set(dimensions))
            return reading()

        registry = CapabilityRegistry(probe=probe(), loader=loader)
        registry.snapshot()
        registry.recheck(RecheckTrigger.ENVIRONMENT_SWITCHED)
        assert requested[1] == set(FIXED_DIMENSIONS)

    def test_session_resume_reprobes_everything(self) -> None:
        """会话恢复后能力不明 → 全量重查。"""
        requested: list[set[CapabilityDimension]] = []

        def loader(dimensions: set[CapabilityDimension]) -> EnvironmentReading:
            requested.append(set(dimensions))
            return reading()

        registry = CapabilityRegistry(probe=probe(), loader=loader)
        registry.snapshot()
        registry.recheck(RecheckTrigger.SESSION_RESUMED)
        assert requested[1] == set(CapabilityDimension)

    def test_recheck_updates_only_the_probed_dimensions(self) -> None:
        """未重查的维度保持原值（只重查变化部分）。"""
        state = {"subagents": True, "models": ("gpt-5-codex",)}

        def loader(dimensions: set[CapabilityDimension]) -> EnvironmentReading:
            return reading(
                subagents=state["subagents"],
                available_models=state["models"],
            )

        registry = CapabilityRegistry(probe=probe(), loader=loader)
        registry.snapshot()
        state["subagents"] = False  # 子代理消失，但模型清单未变
        updated = registry.recheck(RecheckTrigger.TOOL_FAILURE)
        assert updated.subagents is False

    def test_recheck_records_the_trigger(self) -> None:
        registry = CapabilityRegistry(probe=probe(), loader=lambda d: reading())
        registry.snapshot()
        registry.recheck(RecheckTrigger.CONCURRENCY_ANOMALY)
        assert registry.last_trigger is RecheckTrigger.CONCURRENCY_ANOMALY


# --------------------------------------------------------------------------
# TEST-CAP-003 —— 无真实子代理
# --------------------------------------------------------------------------


class TestNoRealSubagent:
    def test_no_subagents_is_reported_truthfully(self) -> None:
        """L17：如实说明，不暂停、不模拟分支。"""
        snapshot = probe().snapshot(reading(subagents=False))
        assert snapshot.subagents is False
        assert snapshot.has_real_subagents is False
        assert snapshot.is_unknown is False

    def test_no_subagents_forces_single_agent_staging(self) -> None:
        """L17：自动改为单代理阶段化执行。"""
        outcome = probe().evaluate(reading(subagents=False))
        assert outcome.mode_hint is ProbeOutcome.ModeHint.STAGED_SINGLE_AGENT
        assert outcome.must_report_truthfully is True
        assert outcome.may_simulate_branches is False

    def test_subagents_present_allows_branching_decision(self) -> None:
        outcome = probe().evaluate(reading(subagents=True))
        assert outcome.mode_hint is ProbeOutcome.ModeHint.NO_CONSTRAINT

    def test_unknown_subagents_does_not_silently_stage(self) -> None:
        """未知 ≠ 不支持：不得把"不知道"当成"不支持"来降级。"""
        snapshot = probe().snapshot(
            reading(subagents=None, unavailable_reason=UnavailableReason.TOOL_FAILURE)
        )
        assert snapshot.is_unknown is True
        assert snapshot.subagents is None

    def test_probe_never_simulates(self) -> None:
        """禁止假分流：outcome 里没有"假装有分支"的出口。"""
        field_names = {f.name for f in dataclasses.fields(ProbeOutcome)}
        for forbidden in ("simulate", "fake_branches", "pretend"):
            assert forbidden not in field_names


# --------------------------------------------------------------------------
# TEST-CAP-004 —— 热切换
# --------------------------------------------------------------------------


class TestHotSwitch:
    def test_hot_switch_supported_means_append_in_place(self) -> None:
        outcome = probe().evaluate(reading(hot_switch=True))
        assert outcome.hot_switch_strategy is ProbeOutcome.HotSwitchStrategy.APPEND

    def test_hot_switch_unsupported_means_stop_and_rebuild(self) -> None:
        """L17：不能热切换时，停止旧分支、复用有效压缩结果后按新配置重建。"""
        outcome = probe().evaluate(reading(hot_switch=False))
        assert (
            outcome.hot_switch_strategy
            is ProbeOutcome.HotSwitchStrategy.STOP_AND_REBUILD
        )
        assert outcome.must_reuse_compressed_results is True
        assert outcome.may_restart_unstarted_plans is False

    def test_hot_switch_unknown_does_not_assume_support(self) -> None:
        snapshot = probe().snapshot(
            reading(hot_switch=None, unavailable_reason=UnavailableReason.NOT_PROBED)
        )
        assert snapshot.hot_switch is None
        assert snapshot.is_unknown is True


# --------------------------------------------------------------------------
# TEST-CAP-005 —— 真实性
# --------------------------------------------------------------------------


class TestNoFabrication:
    def test_capabilities_come_only_from_the_reading(self) -> None:
        """只使用环境真实暴露的能力 —— 快照字段与读数一一对应。"""
        r = reading(
            subagents=False,
            async_background=False,
            concurrency_limit=1,
            available_models=("only-model",),
            available_effort_levels=(EffortLevel.LOW,),
            hot_switch=True,
            reliable_cost_data=True,
        )
        snapshot = probe().snapshot(r)
        assert snapshot.subagents is False
        assert snapshot.async_background is False
        assert snapshot.concurrency_limit == 1
        assert snapshot.available_models == ("only-model",)
        assert snapshot.available_effort_levels == (EffortLevel.LOW,)
        assert snapshot.hot_switch is True
        assert snapshot.reliable_cost_data is True

    def test_false_is_not_the_same_as_unknown(self) -> None:
        """False = 确认不支持；None = 不知道。两者不得混用。"""
        confirmed_off = probe().snapshot(reading(hot_switch=False))
        unknown = probe().snapshot(
            reading(hot_switch=None, unavailable_reason=UnavailableReason.NOT_PROBED)
        )
        assert confirmed_off.hot_switch is False
        assert unknown.hot_switch is None
        assert confirmed_off.is_unknown is False
        assert unknown.is_unknown is True

    def test_reading_requires_every_dimension_explicitly(self) -> None:
        """读数必须显式给出全部 8 项，不允许靠默认值蒙混。"""
        with pytest.raises(TypeError):
            EnvironmentReading(subagents=True)  # type: ignore[call-arg]

    def test_probe_does_not_invent_models(self) -> None:
        outcome = probe().evaluate(reading(available_models=()))
        assert outcome.mode_hint is ProbeOutcome.ModeHint.NO_CONSTRAINT
        snapshot = probe().snapshot(reading(available_models=()))
        assert snapshot.available_models == ()

    def test_capability_module_does_not_touch_platform_surfaces(self) -> None:
        import inspect

        from adaptive_router import capabilities as module

        source = inspect.getsource(module)
        for forbidden in ("os.environ", "subprocess", "requests", "socket"):
            assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# 冻结契约不被行为代码污染
# --------------------------------------------------------------------------


class TestFrozenContractsUntouched:
    def test_capabilities_module_does_not_redefine_contract_types(self) -> None:
        from adaptive_router import capabilities as module
        from adaptive_router.contracts import CapabilitySnapshot

        assert module.CapabilitySnapshot is CapabilitySnapshot

    def test_registry_returns_frozen_contract_types(self) -> None:
        from adaptive_router.contracts import CapabilitySnapshot

        registry = CapabilityRegistry(probe=probe(), loader=lambda d: reading())
        assert isinstance(registry.snapshot(), CapabilitySnapshot)
