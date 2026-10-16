"""images/router/Containerfile is shell and curl; the shape a person reads, without building it."""

from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
RECIPE = (HOME / "images" / "router" / "Containerfile").read_text()
BUILD = (HOME / "scripts" / "build-images.sh").read_text()


def test_recipe_takes_the_rootfs_out_of_the_pinned_iso() -> None:
    assert "ARG VYOS=" in RECIPE and "ARG SHA256=" in RECIPE
    assert "vyos/vyos-nightly-build/releases/download/${VYOS}-rolling/" in RECIPE
    assert "sha256sum -c" in RECIPE
    assert "live/filesystem.squashfs" in RECIPE and "unsquashfs" in RECIPE
    assert "FROM scratch" in RECIPE and "COPY --from=" in RECIPE


def test_recipe_runs_vyos_as_a_container_router() -> None:
    assert 'CMD ["/sbin/init"]' in RECIPE
    assert "STOPSIGNAL SIGRTMIN+3" in RECIPE
    assert "systemctl mask" in RECIPE and "auditd.service" in RECIPE and "rsyslog.service" in RECIPE


def test_build_script_builds_the_router_from_versions_yaml() -> None:
    assert "router)" in BUILD
    assert 'build_push "router:${vyos}" images/router' in BUILD
    assert "--build-arg=VYOS=" in BUILD and "--build-arg=SHA256=" in BUILD
