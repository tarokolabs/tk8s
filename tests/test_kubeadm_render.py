from tk8s.create import kubeadm
from tk8s.host import FakeHost
from tk8s.phases import Context, Runner


def test_init_config_names_cluster_endpoint_and_sans(ctx: Context) -> None:
    text = kubeadm.render_init(ctx)
    assert 'kubernetesVersion: 1.37.1\ncontrolPlaneEndpoint: "api.demo.k8s:6443"' in text
    assert (
        "  certSANs:\n    - api.demo.k8s\n    - 172.22.0.100\n    - 172.22.0.1\n  extraArgs:"
        in text
    )
    assert "advertiseAddress: 172.22.0.1" in text and "name: demo-cp-1" in text
    assert "podSubnet: 10.244.0.0/21\n  serviceSubnet: 10.98.0.0/24" in text


def test_render_phase_writes_into_the_state_dir_mode_600(ctx: Context, host: FakeHost) -> None:
    Runner([kubeadm.PHASE], only="kubeadm/render").run(ctx)
    assert host.files[ctx.state_dir / "init-config.yaml"] == kubeadm.render_init(ctx)
    assert kubeadm.render_done(ctx)
