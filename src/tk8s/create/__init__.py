"""create cluster, phase by phase, in the order they run."""

from ..phases import Phase
from . import kubeadm, plan, preflight, units

PHASES: list[Phase] = [preflight.PHASE, plan.PHASE, units.PHASE, kubeadm.PHASE]
