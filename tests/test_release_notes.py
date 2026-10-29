"""scripts/release-notes.sh renders the notes from versions.yaml and the digests CI produced."""

import subprocess
from pathlib import Path

from tk8s import versions

HOME = Path(__file__).resolve().parents[1]


def test_release_notes_from_versions_and_digests(tmp_path: Path) -> None:
    v = versions.load(HOME / "versions.yaml")
    d, p = v.kubernetes.default, v.kubernetes.previous
    digests = tmp_path / "digests"
    digests.mkdir()
    (digests / f"node-v{d}.digest").write_text(f"ghcr.io/tarokolabs/tk8s/node:v{d}  sha256:aaa\n")
    (digests / f"node-v{p}.digest").write_text(f"ghcr.io/tarokolabs/tk8s/node:v{p}  sha256:bbb\n")
    (digests / f"router-{v.vyos}.digest").write_text(
        f"ghcr.io/tarokolabs/tk8s/router:{v.vyos}  sha256:ccc\n"
    )
    r = subprocess.run(
        [str(HOME / "scripts" / "release-notes.sh"), "v2026.11.0", str(digests)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "TK_VERSION=v2026.11.0 sh" in out
    assert f"| `ghcr.io/tarokolabs/tk8s/node:v{d}` | `sha256:aaa` |" in out
    assert f"| `ghcr.io/tarokolabs/tk8s/router:{v.vyos}` | `sha256:ccc` |" in out
    assert "--ha bgp" in out and "proxy" in out and "BFD" in out  # both router modes described
    for s in (
        f"cilium {v.cilium}",
        f"Gateway API {v.gateway_api}",
        f"gVisor {v.gvisor}",
        f"metrics-server {v.metrics_server}",
        f"local-path-provisioner {v.local_path_provisioner}",
        f"VyOS rolling {v.vyos}",
        f"{d}、{p}",
        f"uv {v.uv}",
        "kubeReserved 500m",  # the node sizing row
        "--hosts",  # the multi-host row
        "1500m／3084Mi",  # noqa: RUF001 (the notes are in Chinese typography)
    ):
        assert s in out, s
    for s in ("libexec", "worker 內", "kube-vip", "go-task", "HAProxy LB"):
        assert s not in out, s
