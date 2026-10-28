"""~/.kube/config holds every tk8s cluster at once: entries named after the cluster, one context
per cluster, so kubectl config use-context switches between them."""

import pytest

from tk8s import kubeconfig

ADMIN = """\
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: Q0E=
    server: https://api.demo.k8s:6443
  name: kubernetes
contexts:
- context:
    cluster: kubernetes
    user: kubernetes-admin
  name: kubernetes-admin@kubernetes
current-context: kubernetes-admin@kubernetes
users:
- name: kubernetes-admin
  user:
    client-certificate-data: Q0VSVA==
    client-key-data: S0VZ
"""


def test_rename_names_everything_after_the_cluster_and_points_at_the_endpoint() -> None:
    text = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    doc = kubeconfig.load(text)
    assert [c["name"] for c in doc["clusters"]] == ["demo"]
    assert doc["clusters"][0]["cluster"]["server"] == "https://172.22.0.253:6443"
    assert doc["clusters"][0]["cluster"]["certificate-authority-data"] == "Q0E="
    assert [u["name"] for u in doc["users"]] == ["demo-admin"]
    assert doc["contexts"] == [
        {"context": {"cluster": "demo", "user": "demo-admin"}, "name": "demo"}
    ]
    assert doc["current-context"] == "demo"
    assert "kubernetes-admin" not in text


def test_rename_refuses_an_admin_conf_that_is_not_kubeadms() -> None:
    with pytest.raises(ValueError, match="one cluster, one user and one context"):
        kubeconfig.rename("apiVersion: v1\nkind: Config\nclusters: []\n", "demo", "https://x")


def test_merge_keeps_the_other_clusters_and_switches_the_current_context() -> None:
    demo = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    other = kubeconfig.rename(ADMIN, "other", "https://172.22.1.253:6443")
    merged = kubeconfig.merge(kubeconfig.merge(None, demo, "demo"), other, "other")
    doc = kubeconfig.load(merged)
    assert [c["name"] for c in doc["clusters"]] == ["demo", "other"]
    assert [u["name"] for u in doc["users"]] == ["demo-admin", "other-admin"]
    assert [c["name"] for c in doc["contexts"]] == ["demo", "other"]
    assert doc["current-context"] == "other"
    again = kubeconfig.load(kubeconfig.merge(merged, demo, "demo"))
    assert [c["name"] for c in again["clusters"]] == ["demo", "other"]  # replaced, not doubled
    assert again["current-context"] == "demo"


def test_merge_leaves_entries_that_are_not_tk8s_clusters_alone() -> None:
    foreign = (
        "apiVersion: v1\nkind: Config\nclusters:\n- cluster: {server: https://corp}\n  name: corp\n"
        "contexts:\n- context: {cluster: corp, user: me}\n  name: corp\ncurrent-context: corp\n"
        "users:\n- name: me\n  user: {token: t}\n"
    )
    demo = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    doc = kubeconfig.load(kubeconfig.merge(foreign, demo, "demo"))
    assert [c["name"] for c in doc["clusters"]] == ["corp", "demo"]
    assert doc["users"][0] == {"name": "me", "user": {"token": "t"}}
    assert doc["current-context"] == "demo"


def test_merge_refuses_to_clobber_a_config_it_cannot_parse() -> None:
    demo = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    with pytest.raises(ValueError, match="not a kubeconfig"):
        kubeconfig.merge("just: text\n", demo, "demo")


def test_remove_drops_the_cluster_and_unsets_the_context_if_it_was_current() -> None:
    demo = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    other = kubeconfig.rename(ADMIN, "other", "https://172.22.1.253:6443")
    merged = kubeconfig.merge(kubeconfig.merge(None, other, "other"), demo, "demo")
    rest = kubeconfig.remove(merged, "demo", "https://172.22.0.253:6443")
    assert rest is not None
    doc = kubeconfig.load(rest)
    assert [c["name"] for c in doc["clusters"]] == ["other"]
    assert [u["name"] for u in doc["users"]] == ["other-admin"]
    assert [c["name"] for c in doc["contexts"]] == ["other"]
    assert doc["current-context"] == ""
    assert kubeconfig.remove(rest, "other", "https://172.22.1.253:6443") is None  # nothing left
    assert kubeconfig.has(merged, "demo", "https://172.22.0.253:6443")
    assert not kubeconfig.has(merged, "nope", "https://x")


def test_rename_is_idempotent_so_a_state_file_from_before_can_be_normalised() -> None:
    once = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    assert kubeconfig.rename(once, "demo", "https://172.22.0.253:6443") == once


def test_merge_refuses_a_same_named_cluster_that_points_elsewhere() -> None:
    foreign = (
        "apiVersion: v1\nkind: Config\nclusters:\n- cluster: {server: https://corp}\n  name: demo\n"
        "contexts:\n- context: {cluster: demo, user: me}\n  name: demo\ncurrent-context: demo\n"
        "users:\n- name: me\n  user: {token: t}\n"
    )
    demo = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    with pytest.raises(ValueError, match="already has a cluster named demo at https://corp"):
        kubeconfig.merge(foreign, demo, "demo")


def test_remove_leaves_a_same_named_cluster_that_points_elsewhere_alone() -> None:
    foreign = (
        "apiVersion: v1\nkind: Config\nclusters:\n- cluster: {server: https://corp}\n  name: demo\n"
        "contexts:\n- context: {cluster: demo, user: demo-admin}\n  name: demo\n"
        "current-context: demo\nusers:\n- name: demo-admin\n  user: {token: t}\n"
    )
    assert kubeconfig.remove(foreign, "demo", "https://172.22.0.253:6443") == foreign
    assert not kubeconfig.has(foreign, "demo", "https://172.22.0.253:6443")
    assert kubeconfig.has(foreign, "demo", "https://corp")


def test_merge_refuses_when_a_foreign_context_or_user_already_uses_our_names() -> None:
    demo = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    context_taken = (
        "apiVersion: v1\nkind: Config\nclusters:\n- cluster: {server: https://corp}\n  name: corp\n"
        "contexts:\n- context: {cluster: corp, user: me}\n  name: demo\ncurrent-context: demo\n"
        "users:\n- name: me\n  user: {token: t}\n"
    )
    with pytest.raises(ValueError, match="already has a context named demo"):
        kubeconfig.merge(context_taken, demo, "demo")
    user_taken = (
        "apiVersion: v1\nkind: Config\nclusters:\n- cluster: {server: https://corp}\n  name: corp\n"
        "contexts:\n- context: {cluster: corp, user: demo-admin}\n  name: corp\n"
        "current-context: corp\nusers:\n- name: demo-admin\n  user: {token: t}\n"
    )
    with pytest.raises(ValueError, match="already has a user named demo-admin"):
        kubeconfig.merge(user_taken, demo, "demo")


def test_null_sections_as_kubectl_leaves_them_are_an_empty_config() -> None:
    emptied = 'apiVersion: v1\nclusters: null\ncontexts: null\ncurrent-context: ""\n'
    emptied += "kind: Config\npreferences: {}\nusers: null\n"
    demo = kubeconfig.rename(ADMIN, "demo", "https://172.22.0.253:6443")
    doc = kubeconfig.load(kubeconfig.merge(emptied, demo, "demo"))
    assert [c["name"] for c in doc["contexts"]] == ["demo"] and doc["preferences"] == {}
    assert not kubeconfig.has(emptied, "demo", "https://172.22.0.253:6443")
