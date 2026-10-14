"""create cluster, phase by phase, in the order they run."""

from ..phases import Phase
from . import plan

PHASES: list[Phase] = [plan.PHASE]
