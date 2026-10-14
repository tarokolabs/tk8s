"""versions.yaml, typed: the single source of pinned component versions."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict


class Kubernetes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    default: str
    previous: str


class Versions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kubernetes: Kubernetes
    crio: dict[str, str]
    cilium: str
    cilium_cli: str
    cni_plugins: str
    calico: str
    metrics_server: str
    local_path_provisioner: str
    gvisor: str
    gateway_api: str
    haproxy: str
    crun: str
    podman: str
    uv: str
    verify_busybox: str
    verify_alpine: str
    verify_agnhost: str

    def crio_for(self, k8s: str) -> str | None:
        """The CRI-O tag for a Kubernetes version's minor; None when versions.yaml has no entry."""
        return self.crio.get(k8s.rsplit(".", 1)[0])


def load(path: Path) -> Versions:
    return Versions.model_validate(yaml.safe_load(path.read_text()))
