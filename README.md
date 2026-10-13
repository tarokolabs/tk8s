# Taroko Kubernetes

> 版本政策：`main` 是開發線；穩定版用最新的 [Release](https://github.com/tarokolabs/tk8s/releases) tag（`TK_VERSION=<tag> sh install.sh`）。v1 世代（`kto`、`conf/`）的最後版本是 [v2026.9.1](https://github.com/tarokolabs/tk8s/releases/tag/v2026.9.1)，不再維護。

在單一 Linux 主機上，以 podman 容器作為節點建立多節點 Kubernetes 叢集。一道指令建起來，不用先改任何設定檔。

> **這個 repo 只有平台。** 教材、工作負載與技術文件在 [tarokolabs/wulin](https://github.com/tarokolabs/wulin)。

## 平台包含什麼

一個「能用的 K8s 叢集」所需的最小集合：

| 元件 | 說明 |
|---|---|
| Kubernetes | kubeadm 建立，預設 **1.37.0**，次新 1.36.4（見 `versions.yaml`） |
| Container runtime | **CRI-O**（版本對齊 K8s minor）；只有一種節點 image |
| CNI | **cilium** 1.20.2，kube-proxy replacement；datapath 預設在核心 ≥ 6.8 用 netkit，`--datapath veth` 可切 |
| 負載平衡 | cilium LB-IPAM 加 L2 announcement，LoadBalancer IP 從節點網段的 `.200`–`.219` 配發 |
| Gateway API | cilium 內建 controller，GatewayClass `cilium`，Gateway API v1.6.1 CRD（experimental channel，含 TCPRoute、UDPRoute） |
| 高可用 | 每個叢集都有 apiserver LB（HAProxy 容器，節點網段 `.100`），所有節點與主機都經它連 control plane；`--control-planes 3` 以上才有多台 control plane（上限 5），多出來的可以先不加入，留給練習 |
| 節點生命週期 | Podman Quadlet 加 systemd：主機重開機叢集自動回來 |
| RuntimeClass | `crun`（CRI-O 內建）；`--gvisor` 的叢集多一個 `gvisor`（handler `runsc`） |
| gVisor（選配） | `--gvisor` 時建叢集當場裝進每個節點，不烤在 image；需要 veth datapath，會自動選 |
| 監控與儲存 | metrics-server（`kubectl top`）、local-path-provisioner（PVC 資料落在 `/opt/taroko/clusters/<名稱>/storage/`） |

管理主機與私有 registry（偵測到教材時部署）與備份還原在 Plan 2b 加回來。

## 架構

三層，各管一件事：

| 層 | 負責 | 學員看到什麼 |
|---|---|---|
| `bin/tkctl` | 把旗標翻成 `cluster.yaml`，呼叫 go-task。一支 bash，沒有叢集邏輯 | `tkctl --help` 一頁 |
| `Taskfile.yaml` 與 `taskfiles/` | 網段分配、渲染節點 unit、kubeadm init 與 join、cilium、日常操作。每個 task 有說明、依賴與「做過就跳過」的判斷 | `task --list`；打開 Taskfile 就是流程 |
| Podman Quadlet | 每個節點一個 systemd 服務，一個叢集一個 target | `systemctl status <叢集>.target` |

建叢集的流程與實體機上的 kubeadm 一致：建網路與節點容器、第一個 control plane `kubeadm init`、其餘節點 `kubeadm join`、裝 CNI。這是教學上想讓學員讀得懂的部分，所以全部是可讀的 shell。

節點 image 為 `ghcr.io/tarokolabs/tk8s/node:v<K8s 版本>`，拉不到時以 `images/node/` 的配方本地建置。

## 需求

| 項目 | 說明 |
|---|---|
| OS | Linux x86_64，**systemd**（Debian 13、Ubuntu 24.04、Fedora 等） |
| 容器引擎 | **podman ≥ 5.4** |
| cgroup | v2 |
| swap | 必須關閉：`sudo swapoff -a`，並註解 `/etc/fstab` 的 swap 行 |
| 權限 | `sudo` 免密碼。只用在必要處：`podman` 與 `systemctl`（節點是系統層級的 Quadlet unit）、寫 `/etc` 的 unit 與 sysctl 檔、寫 `/usr/local/bin`、第一次建立 `/opt/taroko`（之後歸使用者擁有）、刪叢集時清掉節點以 root 寫下的資料 |
| 網路 | 第一次需連外：節點 image、CNI plugins、cilium CLI，以及叢集用的全部 image（`tkctl pull images` 一次拉進共用 store）。之後建叢集不碰外網，見「共用 image store」 |
| 主機防火牆 | 前置檢查會把主機的 `net.bridge.bridge-nf-call-iptables` 設為 0 並寫入 `/etc/sysctl.d/90-tk8s.conf`：節點之間的橋接流量不經主機 iptables，否則裝了 Docker 的主機（FORWARD 預設 DROP）會擋掉 pod 到其他節點的流量 |
| 其他 | `curl`、`git` |

`task` 與 `kubectl` 由 `install.sh` 下載到 `/usr/local/bin`。

## 安裝

```bash
curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/main/install.sh | sh
tkctl create cluster
```

`install.sh` 做四件事，可重跑：檢查前置條件（podman ≥ 5.4、systemd、cgroup v2、swap 關閉、免密碼 sudo、curl、git）；依 `versions.yaml` 下載 `task` 與 `kubectl` 到 `/usr/local/bin` 並驗 checksum；clone 到 `/opt/taroko/tk8s`；把 `tkctl` 連結到 `/usr/local/bin`。`TK_VERSION=v2026.10.4 sh` 釘版本；開發者可以 clone 到任何位置直接用 `bin/tkctl`。

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
tkctl verify cluster <名稱>            # 逐項 PASS/FAIL/SKIP，任一 FAIL 結束碼 1
tkctl add node <叢集> --role worker|control-plane [--cpu N] [--memory SIZE] [--no-join]
tkctl join node <叢集> <節點>          # 對建了但沒加入的節點執行 kubeadm join
tkctl delete node <叢集> <節點> [--yes]
tkctl pull images [--k8s 版本]       # 把建叢集需要的全部 image 拉進共用 store（create 會自動做）
tkctl get images [--k8s 版本]        # 列清單與是否已在 store
tkctl delete images --k8s 版本 [--yes]   # 刪掉該版本的 store；有叢集在用會被拒絕
tkctl version
```

`PATH` 上任何叫 `tkctl-<名字>` 的執行檔會變成 `tkctl <名字>`（規則同 kubectl 的 plugin：最長匹配、內建動詞優先）。

幾個規則：

- 名稱省略是 `tk8s`。名稱進節點名（`tk8s-control-plane`、`tk8s-worker1`）與叢集 DNS 網域（`tk8s.k8s`），格式 `^[a-z][a-z0-9-]{0,15}$`。
- `--control-planes` 要是奇數、最多 5；不論幾台，apiserver 的入口都是 `api.<叢集>.k8s:6443`，由 `.100` 的 HAProxy 容器服務。
- `--defer-join`：第一個以外的 control plane 建起來但不 join，之後用 `tkctl join node` 加入。這是 HA 練習用的。
- `--gvisor` 需要 veth datapath，會自動選；明確指定 `--datapath netkit` 加 `--gvisor` 會被拒絕。
- `--memory` 接受 `4G`、`4096M` 這種寫法，`4Gi` 不行。
- control plane 可以加在任何叢集上（上限 5）。
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

網段自動分配，每個叢集拿一個索引 N，彼此不重疊：節點 `172.22.N.0/24`（閘道 `.254`、apiserver LB `.100`、LB 池 `.200`–`.219`）、pod `10.244.(8N).0/21`、service `10.98.N.0/24`。要自己指定就寫在 `spec.network`，與既有叢集重疊會被擋，`--allow-overlap` 放行。

```
/opt/taroko/clusters/<名稱>/     # 叢集狀態：cluster.yaml、kubeconfig、init-config.yaml、storage/、logs/
/opt/taroko/cni/                 # CNI plugins，所有叢集共用
/opt/taroko/images/<K8s 版本>/   # 共用 image store，節點唯讀掛載；<版本>.conf 是它的 storage.conf
/opt/taroko/clusters/<名稱>/lb/        # LB 的 haproxy.cfg，由 cluster.yaml 渲染
/etc/containers/systemd/<名稱>/  # Quadlet unit：<名稱>.network、<名稱>-lb.container、每個節點一個 .container
/etc/systemd/system/<名稱>.target
/etc/systemd/system/<名稱>-routes.service   # 主機到 pod 與 service 網段的路由，跟 target 一起啟停
```

環境變數一律 `TK_` 前綴：`TK_DATA_DIR` 換掉 `/opt/taroko`、`TK_WULIN_DIR` 指定教材位置、`TK_ASSUME_YES=1` 等同 `--yes`、`TK_OFFLINE=1` 絕不下載（缺 image 直接報錯）、`TK_TASK` 指定 go-task 執行檔。`tkctl --help` 有完整清單。

節點容器是 Quadlet 管的，每次啟停會重建；節點必須保留的 `/var`、`/etc`、`/usr/local/bin` 放在每節點的 named volume（`<節點>-var`、`-etc`、`-usr-local-bin`），`delete` 會一起清掉。

## 共用 image store

建叢集需要的 image（control plane、Cilium、metrics-server、local-path、HAProxy、`verify` 的測試 image）由主機拉一次，放在 `/opt/taroko/images/<K8s 版本>/`，每個節點容器以唯讀掛在 `/var/lib/containers/shared`，CRI-O 把它當 additional image store：查 image 先看這裡，有就直接用，沒有才照常 pull 進節點自己的 `/var`。所以：

- 第一次 `tkctl create cluster` 多花一次下載（約 2 GB、一兩分鐘），之後每個叢集、每個節點都是零下載；`--k8s` 不同版本各有一個目錄。
- 斷網的教室先在有網路時 `tkctl pull images`，上課時 `TK_OFFLINE=1 tkctl create cluster`：缺任何 image 會直接列出來而不是卡在 pull。CI 的 L3 就是這樣建的。
- 唯讀是刻意的：多個 CRI-O 寫同一個 store 沒有安全的用法（節點沒乾淨關機時 `crio wipe` 會清掉整個 storage 目錄）。學員 `kubectl run` 自己拉的 image 進節點本身的 volume，不會污染共用目錄。
- 清單不是手寫的：`scripts/images.sh <版本>` 從 `versions.yaml`、node image 裡的 `kubeadm config images list`、`cilium install --dry-run` 與 `manifests/` 現算，`tkctl get images` 看得到（因此 `get images` 會先確保 node image 與 cilium CLI 在手上，沒有會先拉）。
- 建叢集時主機要套的東西也都在 repo 裡：Gateway API CRD、metrics-server manifest 與 Cilium 的 Helm chart 由 `scripts/vendor-manifests.sh` 放進 `manifests/`，升版時重跑一次；cilium CLI 不會再去 helm.cilium.io 抓 chart。
- 只有 tkctl 寫這個目錄；主機的 `podman image prune`、`podman system reset` 動的是 `/var/lib/containers/storage`，碰不到它。要清就 `tkctl delete images --k8s <版本>`。
- 節點 image 需是 2026-10 之後的配方（掛載點 `/var/lib/containers/shared`）。更早拉的本機快取用 `sudo podman pull ghcr.io/tarokolabs/tk8s/node:v<版本>` 更新；舊 image 建叢集照樣成功，只是不會用到 store。

## apiserver 的入口

每個叢集有一個小容器跟節點一起啟停：`<名稱>-lb`（HAProxy，`.100`），後端是 cluster.yaml 裡的每一台 control plane，`/readyz` 健康檢查、round-robin。`controlPlaneEndpoint` 與憑證都是 `api.<名稱>.k8s:6443`：每個節點把這個名字指到 `.100`（kubelet、Cilium 都走它），主機的 kubeconfig 直接寫 `.100`——就是 kubeadm 文件畫的那張 HA 圖：一個 load balancer 在 control plane 前面，所有客戶都連它。一台 control plane 掛掉，約 6 秒內從後端清單消失，既有連線被切斷、客戶端立刻重連到別台；加減 control plane 時 tkctl 重新渲染 `haproxy.cfg` 並熱重載（既有連線不斷）。

跟 VIP 的差別：沒有 ARP、沒有 lease、沒有 static pod；單 control plane 的叢集也走同一條路，所以 `verify` 的 `[apiserver-lb]` 每個叢集都會檢查。pod 內不需要也解不到 `api.<名稱>.k8s`（叢集內的 CoreDNS 擁有這個網域）：pod 走 `kubernetes` Service 的 ClusterIP。

## 從主機與其他機器連進叢集

- **主機上**：LoadBalancer IP（`.200`–`.219`）與 ClusterIP 直接可達，不用 port-forward。cilium 以 `bpf.lbExternalClusterIP` 讓節點以外的來源也能打 ClusterIP。
- **其他機器**（同一個區網的筆電、或跑叢集的 VM 之外的宿主機）：加一條到節點網段的路由就好，主機會轉送：

  ```bash
  sudo ip route add 172.22.N.0/24 via <主機 IP>     # LoadBalancer IP
  sudo ip route add 10.98.N.0/24 via <主機 IP>      # ClusterIP（選用）
  ```

  `tkctl describe cluster <名稱>` 的 `Access:` 會印出填好的指令。macOS 用 `sudo route -n add 172.22.N.0/24 <主機 IP>`，Windows 用 `route add 172.22.N.0 mask 255.255.255.0 <主機 IP>`。
- 不再有 v1 的 `tkport` 註解與 DNAT 腳本；教材一律用 `type: LoadBalancer` 或 Gateway 曝露服務。

## 支援的 K8s 版本

支援政策：**最新與次新的 K8s minor**，目前為 1.37.x 與 1.36.x，釘在 `versions.yaml`。每次 push 到 `main` 與每個 tag，CI 都會在 GitHub runner 上以這兩個版本各建一個叢集並跑 `tkctl verify cluster`（workflow `l3`）。更舊的版本可以用 `--k8s` 指定，節點 image 會本地建置，但不在主要支援範圍。

## 與 v1 的差異

v1 叢集不自動轉換：以 v2026.9.1 的工具備份或拆除，PVC 資料先備 `storage/`，新版重建。

- 沒有 `conf/*.conf`：拓樸用旗標或 `-f`，網段自動分配。
- 沒有 `kto`、`kls` 這些短命令，也沒有 `tkctl cluster create` 這種名詞在前的寫法。
- 不需要 source profile，不需要 `bc`、`jq`、`envsubst`、`nc`。
- 節點由 systemd 管，主機重開機叢集自動回來；v1 要手動 `kci`。
- 多 control plane 真的會 join；apiserver 入口是每叢集的 HAProxy 與 DNS，不是 VIP（v1 用 kube-vip）。
- 不再有 macvlan 外接節點、`tkport` DNAT 註解、`route-add`、`expose`；對外曝露改用 LoadBalancer 或 Gateway。
- 只支援 systemd 主機，Alpine 不在範圍內；節點只有 CRI-O，不再有 containerd 變體。

## 舊版

本 repo 曾經是 VMware Workstation + Talos Linux 世代（VMTK2024）。該世代的最後狀態保留在 tag [`pre-restructure-2026-07-29`](https://github.com/tarokolabs/tk8s/tree/pre-restructure-2026-07-29)。v1 世代（`kto`、`conf/`）的最後版本是 [v2026.9.1](https://github.com/tarokolabs/tk8s/releases/tag/v2026.9.1)。

## 想參與

- 發現問題、想提需求 → 開 [issue](https://github.com/tarokolabs/tk8s/issues/new)
- 還不確定要不要做、想討論方向 → 開 [Discussion](https://github.com/tarokolabs/tk8s/discussions)
- **不確定該開哪個 → 開 issue 就好**，維護者會幫你轉

進度看 [Taroko Roadmap](https://github.com/orgs/tarokolabs/projects/1)。

## 授權

本專案採 **GPL-2.0-or-later**（GNU GPL v2，或依你的選擇任何更新版本），見 [LICENSE](LICENSE)。
