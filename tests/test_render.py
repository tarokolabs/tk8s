from pathlib import Path

import pytest

from tk8s.render import template

HOME = Path(__file__).resolve().parents[1]


def test_every_template_looks_like_its_product() -> None:
    for f in (p for p in (HOME / "templates").rglob("*") if p.is_file()):
        text = f.read_text()
        assert "{{" not in text and "{%" not in text, f.name  # no template language, only $vars


def test_substitution_and_strictness(tmp_path: Path) -> None:
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "x.conf").write_text("name=$name\nport=$port\n")
    assert template(tmp_path, "x.conf", name="a", port=6443) == "name=a\nport=6443\n"
    with pytest.raises(KeyError):
        template(tmp_path, "x.conf", name="a")
    with pytest.raises(ValueError, match="unused variables \\['extra'\\]"):
        template(tmp_path, "x.conf", name="a", port=1, extra=2)


def test_node_container_has_the_variables_units_will_pass() -> None:
    out = template(
        HOME,
        "node.container",
        cluster="c",
        role="worker",
        name="c-worker-1",
        ip="172.22.0.2",
        after="\nAfter=c-control-plane-1.service",
        image="img",
        domain="c.k8s",
        lb_ip="172.22.0.100",
        cni_dir="/d/cni",
        images_dir="/d/images",
        k8s="1.37.1",
        state_dir="/d/clusters/c",
        wulin_dir="/d/wulin",
        cpu=2,
        memory="4g",
    )
    assert "PartOf=c.target\nAfter=c-control-plane-1.service\n\n[Container]" in out
    assert "Volume=/d/images/1.37.1:/var/lib/containers/shared:ro" in out
    assert "PodmanArgs=--privileged --cgroupns=private --cpus 2 --memory 4g" in out
