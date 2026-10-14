"""create cluster, phase by phase, in the order they run."""

from ..phases import Phase
from . import images, kubeadm, plan, preflight, units

PHASES: list[Phase] = [preflight.PHASE, plan.PHASE, images.PHASE, units.PHASE, kubeadm.PHASE]
