"""Cluster Mesh: tk8s clusters managed from this operator connected with Cilium Cluster Mesh.

checks refuses what cannot be connected, probe tells whether nodes and pods reach each other,
services reads global services and entrypoints, ops does connect and disconnect. This file
imports nothing from tk8s: lifecycle imports describe_mesh, which imports mesh.services, which
runs this file first."""


class MeshError(Exception):
    """A mesh verb that cannot proceed; the message is the whole story."""
