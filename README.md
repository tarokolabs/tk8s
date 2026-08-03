# Taroko Kubernetes

在單一 Linux 主機上，以 podman 容器作為節點建立多節點 Kubernetes 叢集。叢集拓樸用一個設定檔宣告，一道指令建起來。

> **這個 repo 只有平台。** 教材、工作負載與技術文件在 [tarokolabs/wulin](https://github.com/tarokolabs/wulin)。

## 平台包含什麼

一個「能用的 K8s 叢集」所需的最小集合：

| 元件 | 說明 |
|---|---|
| Kubernetes | 以 `kubeadm` 建置，節點為 privileged podman 容器 |
| Container runtime | **CRI-O**（版本對齊 K8s minor；可切換 containerd）。節點 image 自 GHCR 拉取，拉不到時本地自動建置 |
| CNI | **cilium**（預設 1.20.0，kube-proxy replacement 模式；可切換 canal） |
| 負載平衡 | cilium LB-IPAM + L2 announcement——LoadBalancer IP 原生提供（節點網段 .200–.219） |
| 儲存 | local-path-provisioner |
| Gateway API | Envoy Gateway（GatewayClass `eg`；envoy 以 DaemonSet 部署，`externalTrafficPolicy: Local` 保留來源 IP；CRD 採 experimental channel，含 TCPRoute/UDPRoute） |
| 管理主機（選配） | 偵測到教材 repo（`WULIN_DIR`，預設 `/opt/taroko/wulin`）時自動部署 wulin 的管理主機與私有 registry；無教材時建裸叢集 |
| RuntimeClass | CRI-O 路徑：`crun`（套件原生）。containerd 路徑（`K8SCRI=containerd`）：另有 `gvisor`——gVisor 官方僅支援 containerd，不在 CRI-O 路徑提供；CRI-O 上的沙箱容器規劃採 Kata Containers |
| 資源監控 | metrics-server（`kubectl top` 可用） |

**不包含** Prometheus / Grafana 等監控工具、資料庫、物件儲存與應用工作負載——這些在 [wulin](https://github.com/tarokolabs/wulin)。MetalLB 與 ingress-nginx 也移列教材選配（LoadBalancer 與南北向入口已由 cilium LB-IPAM 與 Envoy Gateway 原生涵蓋）。

邊界規則一句話：**tk8s 是讓叢集存在的東西；跑在叢集裡的東西都在 wulin**。因此本 repo 發佈的 image 只有節點 image（`node/crio`、`node/containerd`）；管理主機（admin）、工具底層（toolbox）等叢集內 image 由 wulin 發佈。

## 架構

不是 kind。`bin/kcn` 直接用 podman 建立一個 bridge 網路，再為每個節點建立一個 privileged 容器（固定 IP、限定 CPU 與記憶體）；`bin/kto` 接著在 control-plane 容器內執行 `kubeadm init`，其餘節點以 `kubeadm join` 加入。

節點 image 為 `ghcr.io/tarokolabs/tk8s/node/<runtime>:v<K8s 版本>`（`crio` 或 `containerd`，依 conf 的 `K8SCRI` 自動對應）。拉取不到時（離線、或該版本未發佈），`kcn` 會以 repo 內的同名配方本地建置。

## 需求

| 項目 | 說明 |
|---|---|
| OS | Linux（x86_64）。原生支援 Alpine 與一般發行版 |
| 容器引擎 | **podman**（腳本以 `sudo podman` 呼叫） |
| **swap** | **必須關閉。** `kto` 偵測到 swap 會直接中止 |
| 儲存路徑 | `/opt/taroko/`（自動建立；`TAROKO_HOME` 可覆寫）——叢集狀態、PVC 儲存與工具下載都在這 |
| 權限 | 需要 `sudo` |
| 網路 | 需連外——會下載 CNI plugins、kubectl、cilium CLI、canal manifest、metrics-server |
| 其他指令 | `jq` `bc` `envsubst`（gettext）`nc` `curl` `tar` |
| 核心模組 | `br_netfilter` |

## 安裝位置

**放哪裡都可以。** CLI 依自身位置自我定位，不要求特定安裝路徑（要顯式指定時設 `TK_HOME`）：

```bash
git clone https://github.com/tarokolabs/tk8s.git ~/tk
```

把 `<clone 位置>/bin` 加入 PATH，並套用 shell 環境設定（`profiles/profile.append` 供 Alpine 使用，`profiles/us-profile` 供 Ubuntu 使用；clone 到非 `~/tk` 時調整其中的 `TK_HOME`）。

### 主機側狀態

叢集執行期的狀態與資料收斂在 `/opt/taroko/`（FHS 的 `/opt/<vendor>`；`TAROKO_HOME` 可覆寫）：

| 路徑 | 內容 |
|---|---|
| `clusters/<叢集名>/` | 叢集狀態、PVC 資料（local-path 儲存根）、管理主機素材 |
| `cni/` | 主機側 CNI plugin |
| `wulin/` | 教材 repo 的預設位置（`WULIN_DIR` 可覆寫） |

節點容器內看到的是同一個語彙：主機的 `clusters/<叢集名>` 掛載為節點內的 `/opt/taroko`，教材掛載為 `/opt/taroko/wulin`。

## 使用

統一入口是 `tkctl`（git 式 dispatcher，子命令是可讀的 shell 腳本——想知道平台實際做了什麼，直接打開 `libexec/tkctl/` 對應檔案）：

```bash
tkctl cluster create <叢集名稱> [K8s 版本]   # 建叢集
tkctl cluster list                           # 列出叢集
tkctl help                                   # 全部子命令
```

**既有短命令全數保留**（`kto`、`kls`、`ksc`…），是等價的相容 shim——教材與肌肉記憶不受影響：

```bash
kto <叢集名稱> [K8s 版本]    # 等同 tkctl cluster create
```

不帶參數執行會列出可用的叢集名稱與版本。設定檔（`conf/*.conf`）載入時會驗證必要欄位與格式，缺漏會具名報錯。

## 環境定義

`conf/*.conf` 以 shell 變數宣告叢集拓樸：網段、節點清單（IP、名稱、記憶體、CPU）、K8s 版本、CNI、Gateway 設定。

| 名稱 | 節點數 | 網段 | 預設 K8s 版本 |
|---|---|---|---|
| `tk8s` | 3 | `172.22.0.0/24` | 1.35.5 |
| `tkbp` | 3 | `172.22.8.0/24` | 1.35.5 |
| `tkdt` | 5 | `172.22.16.0/24` | 1.35.5 |
| `tklh` | 3 | `172.22.16.0/24` | 1.35.5 |
| `tkops` | 3 | `172.22.24.0/24` | 1.34.3 |
| `tkdev` | 3 | `172.22.32.0/24` | 1.34.3 |
| `tkha` | 5 | `172.22.64.0/24` | 1.34.8 |
| `tdcs1` | 5 | `172.22.160.0/24` | 1.34.8 |
| `lkh5` | 5 | `172.22.160.0/24` | 1.34.8 |

要新增環境，複製一份 `.conf` 改網段與節點清單即可。

> **注意網段衝突**：`tkdt` 與 `tklh` 共用 `172.22.16.0/24`、`tdcs1` 與 `lkh5` 共用 `172.22.160.0/24`。同一組內的叢集**不能同時存在**——`kcn` 會偵測衝突並中止。

## 支援的 K8s 版本

`bin/init-config-*.yaml` 提供各版本的 kubeadm 設定範本。支援範圍為 **1.31 – 1.36**（kubeadm 設定 API 皆為 `v1beta4`）。

節點 image 依 K8s 版本自 `ghcr.io/tarokolabs/tk8s/node/<runtime>` 拉取；未發佈的版本會於首次使用時以 repo 內配方本地建置，不依賴任何私有 registry。

## 舊版

本 repo 曾經是 VMware Workstation + Talos Linux 世代（VMTK2024）。該世代的最後狀態保留在 tag [`pre-restructure-2026-07-29`](https://github.com/tarokolabs/tk8s/tree/pre-restructure-2026-07-29)。

當時的教材與技術文件已移至 [tarokolabs/wulin](https://github.com/tarokolabs/wulin)，其歷史仍可在本 repo 查詢：

```bash
git log --all -- '技術文件'
```

## 想參與

- 發現問題、想提需求 → 開 [issue](https://github.com/tarokolabs/tk8s/issues/new)
- 還不確定要不要做、想討論方向 → 開 [Discussion](https://github.com/tarokolabs/tk8s/discussions)
- **不確定該開哪個 → 開 issue 就好**，維護者會幫你轉

進度看 [Taroko Roadmap](https://github.com/orgs/tarokolabs/projects/1)。

## 授權

本專案採 **GPL-2.0-or-later**（GNU GPL v2，或依你的選擇任何更新版本），見 [LICENSE](LICENSE)。
