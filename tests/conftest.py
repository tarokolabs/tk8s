"""Shared fixtures: a fake host and a context with one resolved cluster."""

from pathlib import Path

import pytest

from tests.fixtures import HOME, bgp_user, make_ctx, make_multi_ctx, two_router_user
from tk8s.host import FakeHost
from tk8s.paths import Paths
from tk8s.phases import Context


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(data_dir=tmp_path, wulin_dir=tmp_path / "wulin", home=HOME)


@pytest.fixture
def host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def ctx(paths: Paths, host: FakeHost) -> Context:
    """demo: 1 control plane + 2 workers, index 0, the default Kubernetes version."""
    return make_ctx(host, paths.data_dir)


@pytest.fixture
def bgp_ctx(paths: Paths, host: FakeHost) -> Context:
    """demo in bgp mode: 3 control planes (.1 .2 .3) + 1 worker (.4), index 0, VIP 172.23.0.1."""
    return make_ctx(host, paths.data_dir, user=bgp_user())


@pytest.fixture
def two_router_ctx(paths: Paths, host: FakeHost) -> Context:
    """demo in bgp mode with two routers (.251 MASTER-to-be, .252) behind the floating .253: 3
    control planes (.1 .2 .3) + 1 worker (.4), index 0, VIP 172.23.0.1."""
    return make_ctx(host, paths.data_dir, user=two_router_user())


@pytest.fixture
def multi_ctx(paths: Paths, host: FakeHost) -> tuple[Context, dict[str, FakeHost]]:
    """demo over three hosts (a = the operator `host`, b and c remote): bgp, two routers, 3
    control planes + 2 workers, addresses from 192.168.1.64-.95 (tests.fixtures.MULTI_YAML)."""
    return make_multi_ctx(host, paths.data_dir)
