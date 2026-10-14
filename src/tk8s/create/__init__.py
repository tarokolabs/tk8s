"""create cluster, phase by phase, in the order they run."""

from ..phases import Phase
from . import addons, cni, images, kubeadm, nodes, plan, preflight, units

PHASES: list[Phase] = [
    preflight.PHASE,
    plan.PHASE,
    images.PHASE,
    units.PHASE,
    nodes.PHASE,
    kubeadm.PHASE,
    cni.PHASE,
    addons.PHASE,
]

# --dry-run never touches the host: it resolves the plan and renders every file, nothing else.
DRY_RUN_PHASES: list[Phase] = [
    plan.PHASE,
    units.PHASE,
    Phase("kubeadm", kubeadm.PHASE.summary, None, lambda ctx: None, children=(kubeadm.RENDER,)),
]
