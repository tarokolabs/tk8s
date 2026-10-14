import hashlib

import pytest

from tk8s.create import addons
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed, Runner

APT = "apt-get update -qq >/dev/null && apt-get install -y -qq zstd >/dev/null"


def with_gvisor(ctx: Context) -> Context:
    c = ctx.require_cluster()
    spec = c.spec.model_copy(update={"gvisor": True, "datapath": "veth"})
    ctx.cluster = c.model_copy(update={"spec": spec})
    return ctx


def kube(host: FakeHost) -> list[list[str]]:
    return [c.argv for c in host.calls if c.argv[0] == "kubectl"]


def test_runtimeclass_applies_the_manifest_and_touches_no_node(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("kubectl", "get", "runtimeclass", "crun")] = Result(1, "", "")
    assert not addons.runtimeclass_done(ctx)
    addons.runtimeclass_run(ctx)
    manifest = str(ctx.paths.home / "manifests" / "runtimeclass.yaml")
    assert ["kubectl", "apply", "-f", manifest] in kube(host)
    assert not any(c.argv[0] in ("podman", "curl") for c in host.calls)
    assert "  RuntimeClass crun ok" in host.lines


def test_gvisor_disabled_is_done_and_touches_nothing(ctx: Context, host: FakeHost) -> None:
    assert addons.gvisor_done(ctx)
    Runner([addons.PHASE], only="addons/gvisor").run(ctx)
    assert host.lines[-1] == "[addons/gvisor] already done"
    assert not any("gvisor" in " ".join(c.argv) for c in host.calls)


def test_gvisor_downloads_once_verifies_and_installs_into_every_node(
    ctx: Context, host: FakeHost
) -> None:
    ctx = with_gvisor(ctx)
    v = ctx.versions.gvisor
    tar = b"stub-download\n"
    sha = hashlib.sha512(tar).hexdigest()
    for n in ("demo-cp-1", "demo-worker-1", "demo-worker-2"):  # no marker, no zstd, no conf
        host.responses[("podman", "exec", n, "test")] = Result(1, "", "")
    host.responses[("kubectl", "get", "runtimeclass", "gvisor")] = Result(1, "", "")
    cache = ctx.paths.cache_dir
    host.files[cache / "gvisor.tar.zstd"] = tar.decode()
    host.files[cache / "gvisor.tar.zstd.sha512"] = f"{sha}  gvisor.tar.zstd\n"
    addons.gvisor_run(ctx)
    a = [c.argv for c in host.calls]
    url = f"https://storage.googleapis.com/gvisor/releases/release/{v}/x86_64"
    assert [
        "curl",
        "-fsSL",
        "--retry",
        "3",
        "--retry-all-errors",
        "-o",
        str(cache / "gvisor.tar.zstd"),
        f"{url}/gvisor.tar.zstd",
    ] in a
    assert ["mv", str(cache / "gvisor.tar.zstd"), str(cache / f"gvisor-{v}.tar.zstd")] in a
    for n in ("demo-cp-1", "demo-worker-1", "demo-worker-2"):
        assert ["podman", "exec", n, "sh", "-c", APT] in a
        assert [
            "podman",
            "cp",
            str(cache / f"gvisor-{v}.tar.zstd"),
            f"{n}:/tmp/gvisor.tar.zstd",
        ] in a
        assert [
            "podman",
            "exec",
            n,
            "tar",
            "--zstd",
            "-xf",
            "/tmp/gvisor.tar.zstd",
            "-C",
            "/usr/local/bin",
        ] in a
        assert ["podman", "exec", n, "touch", f"/usr/local/bin/.gvisor-{v}"] in a
        conf = str(ctx.paths.home / "templates" / "crio" / "20-gvisor.conf")
        assert ["podman", "cp", conf, f"{n}:/etc/crio/crio.conf.d/20-gvisor.conf"] in a
        assert ["podman", "exec", n, "systemctl", "restart", "crio"] in a
    apt_at = a.index(["podman", "exec", "demo-cp-1", "sh", "-c", APT])
    tar_at = a.index(
        [
            "podman",
            "exec",
            "demo-cp-1",
            "tar",
            "--zstd",
            "-xf",
            "/tmp/gvisor.tar.zstd",
            "-C",
            "/usr/local/bin",
        ]
    )
    assert apt_at < tar_at
    rc = next(c for c in host.calls if c.argv == ["kubectl", "apply", "-f", "-"])
    assert rc.input is not None and "handler: runsc" in rc.input
    assert (
        f"  RuntimeClass gvisor ok (gVisor {v}; datapath must be veth, see --gvisor)" in host.lines
    )


def test_gvisor_checksum_mismatch_fails_and_drops_the_download(
    ctx: Context, host: FakeHost
) -> None:
    ctx = with_gvisor(ctx)
    host.responses[("podman", "exec", "demo-cp-1", "test")] = Result(1, "", "")
    cache = ctx.paths.cache_dir
    host.files[cache / "gvisor.tar.zstd"] = "stub-download\n"
    host.files[cache / "gvisor.tar.zstd.sha512"] = "0" * 128 + "  gvisor.tar.zstd\n"
    with pytest.raises(PhaseFailed, match=r"gvisor\.tar\.zstd checksum mismatch"):
        Runner([addons.PHASE], only="addons/gvisor").run(ctx)
    assert cache / "gvisor.tar.zstd" not in host.files


def test_gvisor_second_run_is_done_and_does_not_restart_crio(ctx: Context, host: FakeHost) -> None:
    ctx = with_gvisor(ctx)
    v = ctx.versions.gvisor
    host.responses[("podman", "exec")] = Result(0, "", "")  # marker and conf present everywhere
    host.responses[("kubectl", "get", "runtimeclass", "gvisor")] = Result(0, "gvisor", "")
    assert addons.gvisor_done(ctx)
    host.responses[("kubectl", "get", "runtimeclass", "gvisor")] = Result(1, "", "")
    assert not addons.gvisor_done(ctx)
    addons.gvisor_run(ctx)
    assert not any(c.argv[-2:] == ["restart", "crio"] for c in host.calls)
    assert not any("tar" in c.argv for c in host.calls)
    assert addons.gvisor_marker(ctx) == f"/usr/local/bin/.gvisor-{v}"


def test_gvisor_node_with_zstd_skips_apt(ctx: Context, host: FakeHost) -> None:
    ctx = with_gvisor(ctx)
    host.responses[("podman", "exec", "demo-cp-1", "test", "-e")] = Result(1, "", "")
    host.responses[("podman", "exec", "demo-cp-1", "test", "-x", "/usr/bin/zstd")] = Result(
        0, "", ""
    )
    host.responses[("podman", "exec")] = Result(0, "", "")
    host.files[ctx.paths.cache_dir / f"gvisor-{ctx.versions.gvisor}.tar.zstd"] = "cached"
    addons.gvisor_run(ctx)
    assert not any("apt-get" in " ".join(c.argv) for c in host.calls)
    assert not any(c.argv[0] == "curl" for c in host.calls)  # cached tarball, no download


def test_metrics_server_applies_patches_and_waits(ctx: Context, host: FakeHost) -> None:
    host.responses[("kubectl", "get", "deploy", "-n", "kube-system", "metrics-server")] = [
        Result(1, "", ""),
        Result(0, '["--secure-port=10250"]', ""),
    ]
    assert not addons.metrics_server_done(ctx)
    addons.metrics_server_run(ctx)
    a = kube(host)
    msv = ctx.versions.metrics_server.lstrip("v")
    manifest = str(ctx.paths.home / "manifests" / f"metrics-server.{msv}.yaml")
    assert ["kubectl", "apply", "-f", manifest] in a
    patch = next(c for c in a if c[:4] == ["kubectl", "-n", "kube-system", "patch"])
    assert '"--kubelet-insecure-tls"' in patch[-1]
    assert [
        "kubectl",
        "rollout",
        "status",
        "-n",
        "kube-system",
        "deploy/metrics-server",
        "--timeout=300s",
    ] in a
    assert f"  metrics-server {ctx.versions.metrics_server} ok" in host.lines


def test_metrics_server_deployed_but_unpatched_is_patched_on_rerun(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("kubectl", "get", "deploy", "-n", "kube-system", "metrics-server")] = Result(
        0, '["--secure-port=10250"]', ""
    )
    assert not addons.metrics_server_done(ctx)
    addons.metrics_server_run(ctx)
    assert any(c[:4] == ["kubectl", "-n", "kube-system", "patch"] for c in kube(host))
    host.responses[("kubectl", "get", "deploy", "-n", "kube-system", "metrics-server")] = Result(
        0, '["--secure-port=10250","--kubelet-insecure-tls"]', ""
    )
    assert addons.metrics_server_done(ctx)


def test_local_path_applies_and_waits(ctx: Context, host: FakeHost) -> None:
    host.responses[("kubectl", "get", "storageclass", "local-path")] = Result(1, "", "")
    assert not addons.local_path_done(ctx)
    addons.local_path_run(ctx)
    lpv = ctx.versions.local_path_provisioner.lstrip("v")
    a = kube(host)
    manifest = str(ctx.paths.home / "manifests" / f"local-path-storage.{lpv}.yaml")
    assert ["kubectl", "apply", "-f", manifest] in a
    assert [
        "kubectl",
        "rollout",
        "status",
        "-n",
        "local-path-storage",
        "deploy/local-path-provisioner",
        "--timeout=300s",
    ] in a
    v = ctx.versions.local_path_provisioner
    assert f"  local-path {v} ok (PV data under {ctx.state_dir}/storage)" in host.lines


def test_rollout_failure_names_the_addon(ctx: Context, host: FakeHost) -> None:
    host.responses[("kubectl", "get", "storageclass", "local-path")] = Result(1, "", "")
    host.responses[("kubectl", "rollout", "status")] = Result(1, "", "timed out")
    with pytest.raises(PhaseFailed, match="local-path-provisioner did not become ready"):
        Runner([addons.PHASE], only="addons/local-path").run(ctx)


def test_phase_order(ctx: Context) -> None:
    names = [p.name for p in addons.PHASE.children]
    assert names == ["runtimeclass", "gvisor", "metrics-server", "local-path"]
