"""Where tk8s keeps things on the host. Every path a phase touches is derived here."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    data_dir: Path  # TK_DATA_DIR, default /opt/taroko; owned by the user running tkctl
    wulin_dir: Path  # TK_WULIN_DIR, default <data_dir>/wulin; mounted into every node
    home: Path  # the tk8s checkout: templates/, manifests/, images/, versions.yaml
    unit_dir: Path = Path("/etc/containers/systemd")
    systemd_dir: Path = Path("/etc/systemd/system")

    @property
    def clusters_dir(self) -> Path:
        return self.data_dir / "clusters"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def cni_dir(self) -> Path:
        return self.data_dir / "cni"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def state_dir(self, cluster: str) -> Path:
        return self.clusters_dir / cluster

    @staticmethod
    def from_env(home: Path) -> Paths:
        data = Path(os.environ.get("TK_DATA_DIR") or "/opt/taroko")
        wulin = Path(os.environ.get("TK_WULIN_DIR") or str(data / "wulin"))
        return Paths(data_dir=data, wulin_dir=wulin, home=home)
