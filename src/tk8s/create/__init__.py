"""create cluster, phase by phase, in the order they run."""

from ..phases import Phase
from . import kubeadm, plan, units

PHASES: list[Phase] = [plan.PHASE, units.PHASE, kubeadm.PHASE]
