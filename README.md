# Taroko Kubernetes

> **這個分支（`v2`）是開發中的下一代 tkctl。** 穩定版請用 [v2026.9.1](https://github.com/tarokolabs/tk8s/releases/tag/v2026.9.1)（`git clone --branch v2026.9.1`）。v2 的安裝腳本、驗證命令、備份還原與教材整合尚未完成，指令與檔案格式在合併前仍可能調整。

在單一 Linux 主機上，以 podman 容器作為節點建立多節點 Kubernetes 叢集。一道指令建起來，不用先改任何設定檔。

> **這個 repo 只有平台。** 教材、工作負載與技術文件在 [tarokolabs/wulin](https://github.com/tarokolabs/wulin)。

## 平台包含什麼

一個「能用的 K8s 叢集」所需的最小集合：

| 元件 | 說明 |
|---|---|
| Kubernetes | kubeadm 建立，預設 **1.37.0**，次新 1.36.4（見 `versions.yaml`） |
| Container runtime | **CRI-O**（版本對齊 K8s minor）；可切換 containerd |
| CNI | **cilium** 1.20.2，kube-proxy replacement；datapath 預設在核心 ≥ 6.8 用 netkit，`--datapath veth` 可切 |
| 負載平衡 | cilium LB-IPAM 加 L2 announcement，LoadBalancer IP 從節點網段的 `.200`–`.219` 配發 |
| Gateway API | cilium 內建 controller，GatewayClass `cilium`，Gateway API v1.6.1 CRD（experimental channel，含 TCPRoute、UDPRoute） |
| 高可用 | `--control-planes 3` 以上自動配 kube-vip VIP；多出來的 control plane 可以先不加入，留給練習 |
| 節點生命週期 | Podman Quadlet 加 systemd：主機重開機叢集自動回來 |

RuntimeClass、metrics-server、local-path 儲存、選配的 gVisor、管理主機與私有 registry（偵測到教材時部署）會在後續的 v2 任務加回來。

## 架構

三層，各管一件事：

| 層 | 負責 | 學員看到什麼 |
|---|---|---|
| `bin/tkctl` | 把旗標翻成 `cluster.yaml`，呼叫 go-task。一支 bash，沒有叢集邏輯 | `tkctl --help` 一頁 |
| `Taskfile.yaml` 與 `taskfiles/` | 網段分配、渲染節點 unit、kubeadm init 與 join、cilium、日常操作。每個 task 有說明、依賴與「做過就跳過」的判斷 | `task --list`；打開 Taskfile 就是流程 |
| Podman Quadlet | 每個節點一個 systemd 服務，一個叢集一個 target | `systemctl status <叢集>.target` |

建叢集的流程與實體機上的 kubeadm 一致：建網路與節點容器、第一個 control plane `kubeadm init`、其餘節點 `kubeadm join`、裝 CNI。這是教學上想讓學員讀得懂的部分，所以全部是可讀的 shell。

節點 image 為 `ghcr.io/tarokolabs/tk8s/node/<runtime>:v<K8s 版本>`，拉不到時以 `images/node/<runtime>` 的配方本地建置。

## 需求

| 項目 | 說明 |
|---|---|
| OS | Linux x86_64，**systemd**（Debian 13、Ubuntu 24.04、Fedora 等） |
| 容器引擎 | **podman ≥ 5.4** |
| cgroup | v2 |
| swap | 必須關閉：`sudo swapoff -a`，並註解 `/etc/fstab` 的 swap 行 |
| 權限 | `sudo` 免密碼 |
| 網路 | 需連外：下載節點 image、CNI plugins、cilium CLI、Gateway API CRD |
| 其他 | `curl`、`git` |

`task` 與 `kubectl` 兩個執行檔目前要自己放到 `/usr/local/bin`（見下一節）；一行安裝腳本會在 v2 完成前補上。

## 安裝（v2 開發期間）

```bash
# 1. 兩個執行檔
v=$(curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/v2/versions.yaml | grep '^task:' | awk '{print $2}' | tr -d '"')
curl -fsSL https://github.com/go-task/task/releases/download/${v}/task_linux_amd64.tar.gz | sudo tar -xz -C /usr/local/bin task
k=$(curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/v2/versions.yaml | grep -A1 '^kubernetes:' | grep default | awk '{print $2}' | tr -d '"')
sudo curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v${k}/bin/linux/amd64/kubectl" && sudo chmod +x /usr/local/bin/kubectl

# 2. 平台
git clone --branch v2 https://github.com/tarokolabs/tk8s.git ~/tk
sudo ln -sf ~/tk/bin/tkctl /usr/local/bin/tkctl

# 3. 第一個叢集：1 個 control plane、2 個 worker、每節點 2 CPU 4G
tkctl create cluster
```

約四分鐘後印出 `cluster tk8s is ready`。接著：

```bash
tkctl use cluster tk8s      # ~/.kube/config 指向它
kubectl get nodes
tkctl get clusters
```

放哪裡都可以，`tkctl` 依自身位置找到 repo；不需要 source 任何 profile。

## 指令

kubectl 風格，動詞在前。全部如下：

```
tkctl create cluster [名稱] [--control-planes N] [--workers N] [--cpu N] [--memory SIZE]
                     [--k8s 版本] [--datapath auto|netkit|veth] [--gvisor] [--defer-join]
                     [-f cluster.yaml] [--allow-overlap] [--dry-run]
tkctl delete cluster <名稱> [--yes]
tkctl get clusters
tkctl describe cluster <名稱> [-o yaml]
tkctl stop cluster <名稱>              # 停下所有節點，狀態與資料保留
tkctl start cluster <名稱>             # 拉起來，等到節點以新的心跳回報 Ready
tkctl use cluster <名稱>               # 切 ~/.kube/config（原檔留在 ~/.kube/config.bak）
tkctl add node <叢集> --role worker|control-plane [--cpu N] [--memory SIZE] [--no-join]
tkctl join node <叢集> <節點>          # 對建了但沒加入的節點執行 kubeadm join
tkctl delete node <叢集> <節點> [--yes]
tkctl version
```

幾個規則：

- 名稱省略是 `tk8s`。名稱進節點名（`tk8s-control-plane`、`tk8s-worker1`）與叢集 DNS 網域（`tk8s.k8s`），格式 `^[a-z][a-z0-9-]{0,15}$`。
- `--control-planes` 要是奇數；大於 1 時自動啟用 kube-vip，VIP 在節點網段的 `.100`。
- `--defer-join`：第一個以外的 control plane 建起來但不 join，之後用 `tkctl join node` 加入。這是 HA 練習用的。
- `--gvisor` 需要 veth datapath，會自動選；明確指定 `--datapath netkit` 加 `--gvisor` 會被拒絕。
- `--memory` 接受 `4G`、`4096M` 這種寫法，`4Gi` 不行。
- 加 control plane 只能加在有 VIP 的叢集（kubeadm 需要 `controlPlaneEndpoint`）。
- `create` 中斷後再跑同一個指令會從沒做完的地方續行；已完成的叢集再 `create` 會被拒絕。
- `--dry-run` 印出解析後的 `cluster.yaml`、全部 Quadlet unit 與 kubeadm 設定，不動主機。

## 叢集定義檔

旗標在內部展開成同一格式，`tkctl describe cluster <名稱> -o yaml` 印的就是它。要個別指定節點規格就寫檔：

```yaml
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata:
  name: tkdt
spec:
  kubernetes: "1.37.0"
  runtime: crio            # crio | containerd
  cni: cilium              # cilium | canal
  datapath: auto           # auto | netkit | veth
  gvisor: false
  nodes:
    - role: control-plane
      cpu: 4
      memory: 4G
    - role: worker
      count: 3
      cpu: 4
      memory: 4G
    - role: worker
      name: tkdt-worker-big
      cpu: 8
      memory: 16G
    - role: control-plane
      count: 2
      join: false            # 建出來、在跑，但不加入（預設 true）
  network:                   # 省略即自動分配
    nodes: 172.22.16.0/24
```

```bash
tkctl create cluster -f tkdt.yaml
```

`nodes` 每項是一組同規格節點，`count` 省略是 1，`name` 省略自動編號。`-f` 不能跟 `--control-planes`、`--workers`、`--cpu`、`--memory` 混用。

## 網段與狀態目錄

網段自動分配，每個叢集拿一個索引 N，彼此不重疊：節點 `172.22.N.0/24`（閘道 `.254`、LB 池 `.200`–`.219`、VIP `.100`）、pod `10.244.(8N).0/21`、service `10.98.N.0/24`。要自己指定就寫在 `spec.network`，與既有叢集重疊會被擋，`--allow-overlap` 放行。

```
/opt/taroko/clusters/<名稱>/     # 叢集狀態：cluster.yaml、kubeconfig、init-config.yaml、storage/、logs/
/opt/taroko/cni/                 # CNI plugins，所有叢集共用
/etc/containers/systemd/<名稱>/  # Quadlet unit：<名稱>.network、每個節點一個 .container
/etc/systemd/system/<名稱>.target
```

節點容器是 Quadlet 管的，每次啟停會重建；節點必須保留的 `/var`、`/etc`、`/usr/local/bin` 放在每節點的 named volume（`<節點>-var`、`-etc`、`-usr-local-bin`），`delete` 會一起清掉。

## 支援的 K8s 版本

支援政策：**最新與次新的 K8s minor**，目前為 1.37.x 與 1.36.x，釘在 `versions.yaml`。更舊的版本可以用 `--k8s` 指定，節點 image 會本地建置，但不在主要支援範圍。

## 與 v1 的差異

- 沒有 `conf/*.conf`：拓樸用旗標或 `-f`，網段自動分配。
- 沒有 `kto`、`kls` 這些短命令，也沒有 `tkctl cluster create` 這種名詞在前的寫法。
- 不需要 source profile，不需要 `bc`、`jq`、`envsubst`、`nc`。
- 節點由 systemd 管，主機重開機叢集自動回來；v1 要手動 `kci`。
- 多 control plane 真的會 join（v1 只放 kube-vip）。
- 不再有 macvlan 外接節點、`tkport` DNAT 註解、`route-add`、`expose`；對外曝露改用 LoadBalancer 或 Gateway。
- 只支援 systemd 主機，Alpine 不在範圍內。

## 舊版

本 repo 曾經是 VMware Workstation + Talos Linux 世代（VMTK2024）。該世代的最後狀態保留在 tag [`pre-restructure-2026-07-29`](https://github.com/tarokolabs/tk8s/tree/pre-restructure-2026-07-29)。v1 世代（`kto`、`conf/`）的最後版本是 [v2026.9.1](https://github.com/tarokolabs/tk8s/releases/tag/v2026.9.1)。

## 想參與

- 發現問題、想提需求 → 開 [issue](https://github.com/tarokolabs/tk8s/issues/new)
- 還不確定要不要做、想討論方向 → 開 [Discussion](https://github.com/tarokolabs/tk8s/discussions)
- **不確定該開哪個 → 開 issue 就好**，維護者會幫你轉

進度看 [Taroko Roadmap](https://github.com/orgs/tarokolabs/projects/1)。

## 授權

本專案採 **GPL-2.0-or-later**（GNU GPL v2，或依你的選擇任何更新版本），見 [LICENSE](LICENSE)。
