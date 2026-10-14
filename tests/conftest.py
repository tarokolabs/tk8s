"""Shared fixtures: a fake host and a context with one resolved cluster."""

from pathlib import Path

import pytest

from tk8s import model, versions
from tk8s.create.plan import resolve
from tk8s.host import FakeHost
from tk8s.paths import Paths
from tk8s.phases import Context, Options

HOME = Path(__file__).resolve().parents[1]


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(data_dir=tmp_path, wulin_dir=tmp_path / "wulin", home=HOME)


@pytest.fixture
def host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def ctx(paths: Paths, host: FakeHost) -> Context:
    """demo: 1 control plane + 2 workers, index 0, the default Kubernetes version."""
    vers = versions.load(HOME / "versions.yaml")
    user = model.UserCluster(
        metadata=model.Metadata(name="demo"),
        spec=model.UserSpec(
            nodes=[model.NodeGroup(role="control-plane"), model.NodeGroup(role="worker", count=2)]
        ),
    )
    c = Context(
        host=host,
        paths=paths,
        versions=vers,
        options=Options(retry_delay=0),
        say=host.lines.append,
    )
    c.cluster = resolve(
        user,
        k8s_default=vers.kubernetes.default,
        existing=None,
        used_indices=set(),
        allow_overlap=False,
        tk8s_version="test",
    )
    return c
