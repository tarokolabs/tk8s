#!/usr/bin/env bash
# Release notes for a tag from versions.yaml and the image digests CI produced.
#   release-notes.sh <tag> <digest-dir>   # prints Markdown
set -eu
tag=$1; digests=$2
cd "$(dirname "$(realpath "$0")")/.."
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
d=$(sed -nE 's/^  default: *"([^"]+)"/\1/p' versions.yaml); p=$(sed -nE 's/^  previous: *"([^"]+)"/\1/p' versions.yaml)
cat <<EOM
## 安裝

\`\`\`bash
curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/$tag/install.sh | TK_VERSION=$tag sh
tkctl create cluster
\`\`\`

需求與使用方式見 [README](https://github.com/tarokolabs/tk8s/blob/$tag/README.md)。

## 本版 image 對照表

節點 image 由本 tag 的配方於 CI 建置（tag 對應 K8s 版本；以 digest 引用可完全固定內容）。叢集內 image（toolbox、admin 等）屬教材範疇，由 [wulin](https://github.com/tarokolabs/wulin) 發佈。

| image | digest |
|---|---|
$(sort "$digests"/*.digest | awk '{printf "| `%s` | `%s` |\n", $1, $2}')

## 平台元件

| 元件 | 版本 |
|---|---|
| Kubernetes（kubeadm）| 最新與次新 minor：${d}、${p}（更舊版本可本地建置）|
| Container runtime | CRI-O（對齊 K8s minor；單一節點 image）|
| CNI | cilium $(v cilium)（kube-proxy replacement、LB-IPAM、L2 announcement、netkit/veth）|
| Gateway API | cilium 內建 controller（Gateway API $(v gateway_api)，experimental channel CRD）|
| 高可用 | kube-vip $(v kube_vip)（\`--control-planes 3\`）|
| RuntimeClass | crun（CRI-O 內建）；gVisor $(v gvisor)（\`--gvisor\`，建叢集時裝進節點）|
| 監控與儲存 | metrics-server $(v metrics_server)、local-path-provisioner $(v local_path_provisioner) |
| 工具 | go-task $(v task)、kubectl ${d}（install.sh 下載）|
EOM
