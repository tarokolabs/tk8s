"""~/.kube/config with every tk8s cluster in it at once.

kubeadm's admin.conf names its cluster `kubernetes`, its user `kubernetes-admin` and its context
`kubernetes-admin@kubernetes`, the same for every cluster, so two of them could only replace each
other. tkctl renames the entries after the cluster (cluster <name>, user <name>-admin, context
<name>) when it hands the host its kubeconfig; `use cluster` then merges that file into
~/.kube/config and makes the context current, and `delete cluster` takes the entries out again.
Entries named differently (someone else's clusters) are left alone.
"""

from typing import Any

import yaml

Doc = dict[str, Any]
SECTIONS = ("clusters", "users", "contexts")


def load(text: str) -> Doc:
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or doc.get("kind") != "Config":
        raise ValueError("not a kubeconfig (kind: Config)")
    for s in SECTIONS:
        # kubectl writes `clusters: null` once it has deleted the last entry
        if not isinstance(doc.get(s) or [], list):
            raise ValueError(f"not a kubeconfig ({s} is not a list)")
        doc[s] = doc.get(s) or []
    return doc


def dump(doc: Doc) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def names(name: str) -> dict[str, str]:
    return {"clusters": name, "users": f"{name}-admin", "contexts": name}


def rename(admin: str, name: str, server: str) -> str:
    """kubeadm's admin.conf as the host's kubeconfig for cluster `name`: entries named after the
    cluster, the server replaced by what api.<cluster>.k8s means on the host."""
    doc = load(admin)
    if any(len(doc[s]) != 1 for s in SECTIONS):
        raise ValueError("admin.conf should hold one cluster, one user and one context")
    want = names(name)
    doc["clusters"][0]["name"] = want["clusters"]
    doc["clusters"][0]["cluster"]["server"] = server
    doc["users"][0]["name"] = want["users"]
    doc["contexts"][0] = {
        "context": {"cluster": want["clusters"], "user": want["users"]},
        "name": want["contexts"],
    }
    doc["current-context"] = want["contexts"]
    return dump(doc)


def _without(doc: Doc, name: str) -> Doc:
    want = names(name)
    for s in SECTIONS:
        doc[s] = [e for e in doc[s] if e.get("name") != want[s]]
    if doc.get("current-context") == want["contexts"]:
        doc["current-context"] = ""
    return doc


def server_of(doc: Doc, name: str) -> str | None:
    """The server of the cluster entry called `name`, None when there is none."""
    for e in doc["clusters"]:
        if e.get("name") == name:
            return str(e.get("cluster", {}).get("server", ""))
    return None


def merge(existing: str | None, cluster: str, name: str) -> str:
    """`existing` (~/.kube/config, maybe absent) with cluster `name`'s entries from `cluster`
    replacing any of the same name in place (appended when new), and `name` as the current
    context. A cluster of the same name that points elsewhere is somebody else's: refused."""
    doc = load(existing) if existing else load("kind: Config\n")
    new, want = load(cluster), names(name)
    mine, theirs = server_of(new, want["clusters"]), server_of(doc, want["clusters"])
    if theirs is not None and theirs != mine:
        raise ValueError(
            f"already has a cluster named {name} at {theirs} (not this cluster); "
            "move that entry out of the way"
        )
    if theirs is None:
        # no cluster of ours in the file: a context or user carrying our names is somebody else's
        for s, label in (("contexts", "context"), ("users", "user")):
            if any(e.get("name") == want[s] for e in doc[s]):
                raise ValueError(
                    f"already has a {label} named {want[s]} that is not this cluster's; "
                    "move that entry out of the way"
                )
    for s in SECTIONS:
        fresh = [e for e in new[s] if e.get("name") == want[s]]
        slots = [i for i, e in enumerate(doc[s]) if e.get("name") == want[s]]
        if slots:
            doc[s][slots[0] : slots[0] + 1] = fresh
            doc[s] = [e for i, e in enumerate(doc[s]) if i <= slots[0] or e.get("name") != want[s]]
        else:
            doc[s] += fresh
    doc["current-context"] = want["contexts"]
    return dump(doc)


def has(existing: str, name: str, server: str) -> bool:
    """Cluster `name` is in `existing` and it is this cluster (same server)."""
    return server_of(load(existing), names(name)["clusters"]) == server


def remove(existing: str, name: str, server: str) -> str | None:
    """`existing` without cluster `name`, when it is this cluster (same server); unchanged
    otherwise. None when nothing is left in it."""
    if not has(existing, name, server):
        return existing
    doc = _without(load(existing), name)
    if not any(doc[s] for s in SECTIONS):
        return None
    return dump(doc)
