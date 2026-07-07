from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

import pytest


PHASES = ("setup", "call", "teardown")


@dataclass(frozen=True)
class PhaseMetric:
    """Метрики одной фазы одного test item."""

    outcome: str
    duration: float
    start: float
    stop: float


@dataclass
class TestItemMetric:
    """Метрики одного конкретного pytest Item.

    Например:
        tests/test_algo.py::test_sort[10]
    """

    nodeid: str
    group_nodeid: str
    name: str
    path: str
    param_id: str | None
    params: dict[str, str]
    phases: dict[str, PhaseMetric] = field(default_factory=dict)
    outcome: str | None = None

    @property
    def total_duration(self) -> float:
        return sum(phase.duration for phase in self.phases.values())


class TestOpsTimingPlugin:
    def __init__(self) -> None:
        self.items: dict[str, TestItemMetric] = {}
        self.groups: dict[str, list[str]] = {}

    def pytest_collection_modifyitems(
        self,
        session: pytest.Session,
        config: pytest.Config,
        items: list[pytest.Item],
    ) -> None:
        print(f"[testops] collected {len(items)} item(s)")

        for item in items:
            self._register_item(item)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when not in PHASES:
            return

        metric = self.items.get(report.nodeid)

        if metric is None:
            # Нормально такого обычно не должно быть, потому что item должен
            # быть зарегистрирован на collection phase. Но fallback полезен
            # для отладки и странных edge cases.
            metric = TestItemMetric(
                nodeid=report.nodeid,
                group_nodeid=self._fallback_group_nodeid(report.nodeid),
                name=report.nodeid.rsplit("::", maxsplit=1)[-1],
                path="<unknown>",
                param_id=None,
                params={},
            )
            self.items[report.nodeid] = metric
            self.groups.setdefault(metric.group_nodeid, []).append(report.nodeid)

        metric.phases[report.when] = PhaseMetric(
            outcome=report.outcome,
            duration=report.duration,
            start=report.start,
            stop=report.stop,
        )

        self._update_item_outcome(metric, report)

        print(
            "[testops] phase "
            f"nodeid={report.nodeid} "
            f"when={report.when} "
            f"outcome={report.outcome} "
            f"duration={report.duration:.6f}s"
        )

    def pytest_runtest_logfinish(
        self,
        nodeid: str,
        location: tuple[str, int | None, str],
    ) -> None:
        metric = self.items.get(nodeid)

        if metric is None:
            return

        self._print_item_summary(metric)

    def pytest_sessionfinish(
        self,
        session: pytest.Session,
        exitstatus: int,
    ) -> None:
        print()
        print("[testops] session summary")
        print(f"[testops] exitstatus={exitstatus}")
        print(f"[testops] collected={session.testscollected}")
        print()

        for group_nodeid in self.groups:
            self._print_group_summary(group_nodeid)

    def _register_item(self, item: pytest.Item) -> None:
        param_id, params = self._extract_param_data(item)
        group_nodeid = self._build_group_nodeid(item, param_id)

        metric = TestItemMetric(
            nodeid=item.nodeid,
            group_nodeid=group_nodeid,
            name=item.name,
            path=str(item.path),
            param_id=param_id,
            params=params,
        )

        self.items[item.nodeid] = metric
        self.groups.setdefault(group_nodeid, []).append(item.nodeid)

        print(
            "[testops] item "
            f"nodeid={metric.nodeid} "
            f"group={metric.group_nodeid} "
            f"param_id={metric.param_id} "
            f"params={metric.params}"
        )

    @staticmethod
    def _extract_param_data(item: pytest.Item) -> tuple[str | None, dict[str, str]]:
        """Достаем информацию о параметризации, если она есть.

        Для обычного теста:
            param_id = None
            params = {}

        Для параметризованного теста:
            param_id = "10"
            params = {"n": "10"}
        """

        callspec = getattr(item, "callspec", None)

        if callspec is None:
            return None, {}

        param_id = getattr(callspec, "id", None)

        raw_params: dict[str, Any] = getattr(callspec, "params", {})
        params = {
            name: repr(value)
            for name, value in raw_params.items()
        }

        return param_id, params

    @staticmethod
    def _build_group_nodeid(item: pytest.Item, param_id: str | None) -> str:
        """Строим id логического теста без конкретного параметра.

        Было:
            tests/test_algo.py::test_sort[10]

        Станет:
            tests/test_algo.py::test_sort
        """

        if param_id is None:
            return item.nodeid

        suffix = f"[{param_id}]"

        if item.nodeid.endswith(suffix):
            return item.nodeid[: -len(suffix)]

        return TestOpsTimingPlugin._fallback_group_nodeid(item.nodeid)

    @staticmethod
    def _fallback_group_nodeid(nodeid: str) -> str:
        if nodeid.endswith("]") and "[" in nodeid:
            return nodeid.rsplit("[", maxsplit=1)[0]

        return nodeid

    @staticmethod
    def _update_item_outcome(
        metric: TestItemMetric,
        report: pytest.TestReport,
    ) -> None:
        """Определяем итоговый outcome для test item.

        Правила:
        - если setup failed/skipped, call может не выполниться;
        - если call прошел/упал/skipped, это основной outcome;
        - если teardown failed, итоговый item лучше считать failed.
        """

        if report.when == "setup" and report.outcome in {"failed", "skipped"}:
            metric.outcome = report.outcome
            return

        if report.when == "call":
            metric.outcome = report.outcome
            return

        if report.when == "teardown" and report.outcome == "failed":
            metric.outcome = "failed"

    @staticmethod
    def _print_item_summary(metric: TestItemMetric) -> None:
        print(f"[testops] item summary nodeid={metric.nodeid}")
        print(f"[testops]   group={metric.group_nodeid}")
        print(f"[testops]   outcome={metric.outcome}")
        print(f"[testops]   param_id={metric.param_id}")
        print(f"[testops]   params={metric.params}")

        for phase_name in PHASES:
            phase = metric.phases.get(phase_name)

            if phase is None:
                print(f"[testops]   {phase_name}: <missing>")
                continue

            print(
                f"[testops]   {phase_name}: "
                f"outcome={phase.outcome} "
                f"duration={phase.duration:.6f}s"
            )

        print(f"[testops]   total={metric.total_duration:.6f}s")

    def _print_group_summary(self, group_nodeid: str) -> None:
        nodeids = self.groups[group_nodeid]
        metrics = [self.items[nodeid] for nodeid in nodeids]

        print(f"[testops] group summary group={group_nodeid}")
        print(f"[testops]   item_count={len(metrics)}")

        outcome_counts: dict[str, int] = {}

        for metric in metrics:
            outcome = metric.outcome or "<unknown>"
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

        print(f"[testops]   outcomes={outcome_counts}")

        for phase_name in PHASES:
            durations = [
                metric.phases[phase_name].duration
                for metric in metrics
                if phase_name in metric.phases
            ]

            if not durations:
                print(f"[testops]   {phase_name}: <no data>")
                continue

            print(
                f"[testops]   {phase_name}: "
                f"count={len(durations)} "
                f"total={sum(durations):.6f}s "
                f"mean={statistics.mean(durations):.6f}s "
                f"min={min(durations):.6f}s "
                f"max={max(durations):.6f}s"
            )

        totals = [metric.total_duration for metric in metrics]

        if totals:
            print(
                f"[testops]   all_phases: "
                f"count={len(totals)} "
                f"total={sum(totals):.6f}s "
                f"mean={statistics.mean(totals):.6f}s "
                f"min={min(totals):.6f}s "
                f"max={max(totals):.6f}s"
            )

        print()


def pytest_configure(config: pytest.Config) -> None:
    plugin = TestOpsTimingPlugin()
    config.pluginmanager.register(plugin, name="testops-timing-plugin")
