from __future__ import annotations

import json
import statistics
import sys

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


PHASES = ("setup", "call", "teardown")
SCHEMA_VERSION = "testops.run.v1"


@dataclass(frozen=True)
class PhaseMetric:
    outcome: str
    duration: float
    start: float
    stop: float


@dataclass
class TestItemMetric:
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


@dataclass(frozen=True)
class DurationStats:
    count: int
    total: float
    mean: float
    min: float
    max: float


@dataclass(frozen=True)
class TestGroupMetric:
    group_nodeid: str
    item_nodeids: list[str]
    item_count: int
    outcome_counts: dict[str, int]
    phase_stats: dict[str, DurationStats | None]
    all_phases_stats: DurationStats | None


@dataclass(frozen=True)
class ExcludedItemMetric:
    nodeid: str
    reason: str


@dataclass(frozen=True)
class TestRunMetric:
    schema_version: str
    generated_at: str
    rootpath: str
    exitstatus: int
    collected: int
    measured_items: int
    excluded_items: list[ExcludedItemMetric]
    items: list[TestItemMetric]
    groups: list[TestGroupMetric]


class TestOpsTimingPlugin:
    def __init__(
        self,
        *,
        output_path: str | None,
        rootpath: Path,
    ) -> None:
        self.output_path = output_path
        self.rootpath = rootpath

        self.items: dict[str, TestItemMetric] = {}
        self.groups: dict[str, list[str]] = {}

        # Items, которые мы намеренно не включаем в метрики.
        self.excluded_nodeids: set[str] = set()
        self.exclude_reasons: dict[str, str] = {}

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        for item in session.items:
            if self._is_unconditional_skip(item):
                self._exclude_item(
                    nodeid=item.nodeid,
                    reason="@pytest.mark.skip",
                )
                continue

            self._register_item(item)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when not in PHASES:
            return

        if report.nodeid in self.excluded_nodeids:
            return

        if report.outcome == "skipped":
            self._exclude_item(
                nodeid=report.nodeid,
                reason=f"runtime skipped during {report.when}",
            )
            return

        metric = self.items.get(report.nodeid)

        if metric is None:
            # Fallback на случай странного порядка хуков или нестандартного плагина.
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

    def pytest_sessionfinish(
        self,
        session: pytest.Session,
        exitstatus: int,
    ) -> None:
        if self.output_path is None:
            return

        report = self._build_session_report(
            session=session,
            exitstatus=exitstatus,
        )
        payload = json.dumps(
            asdict(report),
            ensure_ascii=False,
            indent=2,
        )

        if self.output_path == "-":
            sys.stdout.write(payload)
            sys.stdout.write("\n")
            return

        output_path = self._resolve_output_path(self.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")

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

    def _exclude_item(self, nodeid: str, reason: str) -> None:
        self.excluded_nodeids.add(nodeid)
        self.exclude_reasons[nodeid] = reason

        metric = self.items.pop(nodeid, None)
        if metric is None:
            return

        group_nodeids = self.groups.get(metric.group_nodeid, [])
        if nodeid in group_nodeids:
            group_nodeids.remove(nodeid)

        if not group_nodeids:
            self.groups.pop(metric.group_nodeid, None)

    def _build_session_report(
        self,
        *,
        session: pytest.Session,
        exitstatus: int,
    ) -> TestRunMetric:
        groups: list[TestGroupMetric] = []

        for group_nodeid in self.groups:
            group_metric = self._build_group_metric(group_nodeid)
            if group_metric is not None:
                groups.append(group_metric)

        return TestRunMetric(
            schema_version=SCHEMA_VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            rootpath=str(self.rootpath),
            exitstatus=exitstatus,
            collected=session.testscollected,
            measured_items=len(self.items),
            excluded_items=[
                ExcludedItemMetric(
                    nodeid=nodeid,
                    reason=self.exclude_reasons.get(nodeid, "<unknown>"),
                )
                for nodeid in sorted(self.excluded_nodeids)
            ],
            items=list(self.items.values()),
            groups=groups,
        )

    def _build_group_metric(self, group_nodeid: str) -> TestGroupMetric | None:
        nodeids = [
            nodeid
            for nodeid in self.groups[group_nodeid]
            if nodeid not in self.excluded_nodeids and nodeid in self.items
        ]
        metrics = [self.items[nodeid] for nodeid in nodeids]

        if not metrics:
            return None

        outcome_counts: dict[str, int] = {}
        for metric in metrics:
            outcome = metric.outcome or "<unknown>"
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

        phase_stats: dict[str, DurationStats | None] = {}

        for phase_name in PHASES:
            durations = [
                metric.phases[phase_name].duration
                for metric in metrics
                if phase_name in metric.phases
            ]
            phase_stats[phase_name] = self._build_duration_stats(durations)

        total_durations = [metric.total_duration for metric in metrics]

        return TestGroupMetric(
            group_nodeid=group_nodeid,
            item_nodeids=nodeids,
            item_count=len(metrics),
            outcome_counts=outcome_counts,
            phase_stats=phase_stats,
            all_phases_stats=self._build_duration_stats(total_durations),
        )

    def _resolve_output_path(self, output_path: str) -> Path:
        path = Path(output_path)

        if path.is_absolute():
            return path

        return self.rootpath / path

    @staticmethod
    def _build_duration_stats(durations: list[float]) -> DurationStats | None:
        if not durations:
            return None

        return DurationStats(
            count=len(durations),
            total=sum(durations),
            mean=statistics.mean(durations),
            min=min(durations),
            max=max(durations),
        )

    @staticmethod
    def _is_unconditional_skip(item: pytest.Item) -> bool:
        return item.get_closest_marker("skip") is not None

    @staticmethod
    def _extract_param_data(item: pytest.Item) -> tuple[str | None, dict[str, str]]:
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
        if report.when == "setup" and report.outcome == "failed":
            metric.outcome = "failed"
            return

        if report.when == "call":
            metric.outcome = report.outcome
            return

        if report.when == "teardown" and report.outcome == "failed":
            metric.outcome = "failed"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("testops")

    group.addoption(
        "--testops-json-report",
        action="store",
        dest="testops_json_report",
        default=None,
        metavar="PATH",
        help=(
            "Write pytest-testops run report to JSON file. "
            "Use '-' to write JSON to stdout."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    plugin = TestOpsTimingPlugin(
        output_path=config.getoption("testops_json_report"),
        rootpath=config.rootpath,
    )
    config.pluginmanager.register(plugin, name="testops-timing-plugin")