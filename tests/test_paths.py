from pathlib import Path

import pytest

from tk8s.paths import Paths


def test_everything_hangs_off_the_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TK_DATA_DIR", "/srv/tk")
    monkeypatch.delenv("TK_WULIN_DIR", raising=False)
    p = Paths.from_env(Path("/opt/taroko/tk8s"))
    assert p.clusters_dir == Path("/srv/tk/clusters")
    assert p.images_dir == Path("/srv/tk/images")
    assert p.cni_dir == Path("/srv/tk/cni")
    assert p.cache_dir == Path("/srv/tk/cache")
    assert p.wulin_dir == Path("/srv/tk/wulin")
    assert p.state_dir("demo") == Path("/srv/tk/clusters/demo")
    assert p.unit_dir == Path("/etc/containers/systemd")


def test_defaults_are_opt_taroko(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TK_DATA_DIR", raising=False)
    monkeypatch.setenv("TK_WULIN_DIR", "/home/x/wulin")
    p = Paths.from_env(Path("/h"))
    assert p.data_dir == Path("/opt/taroko") and p.wulin_dir == Path("/home/x/wulin")
