# Taroko Kubernetes

在單一 Linux 主機上，以 podman 容器作為節點建立多節點 Kubernetes 叢集。一道指令完成建立，不需要事先修改任何設定檔。

> **這個 repo 只有平台本身。** 教材、工作負載與技術文件在 [tarokolabs/wulin](https://github.com/tarokolabs/wulin)。
> `main` 是開發分支；穩定版用最新 [Release](https://github.com/tarokolabs/tk8s/releases) 的 tag。

## 需求

| 項目 | 說明 |
|---|---|
| OS | Linux x86_64，**systemd**（Debian 13、Ubuntu 24.04、Fedora 等） |
| 容器引擎 | **podman ≥ 5.4** |
| cgroup | v2 |
| swap | 關閉：`sudo swapoff -a`，並註解 `/etc/fstab` 的 swap 行 |
| 權限 | `sudo` 免密碼；每一道 sudo 指令執行時都會印出 |
| 網路 | 第一次需要對外連線，下載約 3 GB（含 router image）；之後建叢集不需要網路 |
| 其他 | `curl`、`git` |

## 安裝

```bash
curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/main/install.sh | sh
```

指定版本：`curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/v2026.10.9/install.sh | TK_VERSION=v2026.10.9 sh`。

`install.sh` 可重複執行：檢查前置條件，下載 `uv` 與 `kubectl` 到 `/usr/local/bin`，clone 到 `/opt/taroko/tk8s`，把 Python 3.14 與相依套件安裝到 `/opt/taroko/tk8s/.venv/`，把 `tkctl` 連結到 `/usr/local/bin`。不需要 source 任何 profile。

## 建第一個叢集

```bash
tkctl create cluster
```

約三分鐘後印出 `cluster tk8s is ready`。接著：

```bash
tkctl use cluster tk8s      # ~/.kube/config 指向它（原檔留在 ~/.kube/config.bak）
kubectl get nodes
```

名稱省略是 `tk8s`，節點是 `tk8s-control-plane-1`、`tk8s-worker-1`、`tk8s-worker-2`，另有一個 VyOS 的 `tk8s-router`：節點的預設閘道，也是 apiserver 的負載平衡。其他常見的拓樸：

```bash
tkctl create cluster demo --workers 3                  # 名稱 demo，三台 worker
tkctl create cluster ha --control-planes 3              # 三台 control plane，HA
tkctl create cluster ha --control-planes 3 --defer-join # 額外的 control plane 建好但不加入，供 HA 練習
tkctl create cluster ha --control-planes 3 --ha bgp     # 同樣三台，apiserver 入口改走 BGP + ECMP 的 anycast VIP
tkctl create cluster ha --preset ha                      # 3 control plane + 3 worker、bgp、兩台 router；[plan] 會印出展開的旗標
tkctl create cluster demo --routers 2                    # 兩台 VyOS router 跑 VRRP，.253 在兩台之間漂移；停一台叢集照常
tkctl create cluster sandbox --gvisor                   # 多一個 gvisor 的 RuntimeClass
tkctl create cluster old --k8s 1.36.5                   # 次新版
tkctl create cluster big --cpu 4 --memory 8G
tkctl create cluster bare --skip-phases cni,addons    # 只做到 kubeadm，CNI 自己裝（叢集停在 Creating）
```

### 執行輸出

```
[preflight] check the host: podman, systemd, cgroup v2, swap, sudo, sysctls, data tree, node sizes
  preflight ok: podman 5.4.2, systemd, cgroup v2, swap off, host bridge-nf off, ip_forward on, /opt/taroko owned by student, host 8 cpu, 32873260 kB
[plan] resolve cluster.yaml: defaults, subnet, node names
  cluster demo: 1 control plane + 2 workers, K8s 1.37.1, subnet 172.22.0.0/24
[images] already done
[units] write the Quadlet units, the target, the routes and the LB config
  $ sudo -n install -D -m 644 /dev/stdin /etc/containers/systemd/demo/demo.network
  …
[kubeadm/init] kubeadm init on the first control plane
  $ sudo -n podman exec demo-control-plane-1 kubeadm init --config=/opt/taroko/init-config.yaml --upload-certs --ignore-preflight-errors=SystemVerification
  demo-control-plane-1 initialized
…
cluster demo is ready
```

每個 `[phase]` 一行，其下每道指令一行 `$ …`（寫入檔案的內容用 `--dry-run` 看）；`already done` 表示這一步先前已完成。中斷後再執行同一個指令，已完成的跳過、未完成的接著做；`tkctl describe cluster demo` 的 `Status:` 列出哪些 phase 何時完成、下一步是什麼。

步驟的切法仿照 kubeadm 的 phase，`--phase`、`--skip-phases`、`--list-phases` 的用法也一樣：

```
$ tkctl create cluster --list-phases
preflight          check the host: podman, systemd, cgroup v2, swap, sudo, sysctls, data tree, node sizes
plan               resolve cluster.yaml: defaults, subnet, node names
images             pull every image the cluster needs into the shared store
units              write the Quadlet units, the target, the routes and the router config
nodes              start the nodes
  cni-plugins        install the CNI plugins the nodes mount
  up                 start the cluster target and wait for every node and the router
kubeadm            initialise the first control plane and join the others
  render             write the kubeadm init configuration into the state dir
  frr                bgp mode: FRR and the VIP health timer on every control plane
  init               kubeadm init on the first control plane
  kubeconfig         hand the host a kubeconfig that points at the router (proxy) or the VIP (bgp)
  join               join the other control planes and the workers
cni                install the CNI and wait for the nodes
  install            install the CNI (cilium: kube-proxy replacement, LB-IPAM, Gateway API)
  nodes-ready        wait for every node to be Ready
addons             RuntimeClass, gVisor, metrics-server, local-path
  runtimeclass       RuntimeClass crun (CRI-O's default runtime)
  gvisor             gVisor into every node and RuntimeClass gvisor (only with --gvisor)
  metrics-server     metrics-server (kubectl top)
  local-path         local-path-provisioner with PV data under the state dir
```

`--phase kubeadm` 只執行那一棵子樹（`--phase init` 只執行那一步），`--skip-phases addons` 跳過，`--dry-run` 印出解析後的 `cluster.yaml`、全部 Quadlet unit 與 kubeadm 設定，不變更主機。

## 日常操作

```bash
tkctl get clusters                 # 全部叢集：節點數、版本、狀態、網段
tkctl describe cluster demo        # 節點、位址、Status、Router、供其他機器連線的路由指令
tkctl verify cluster demo          # 逐項 PASS/FAIL/SKIP，任一 FAIL 結束碼 1
tkctl stop cluster demo            # 停止所有節點，狀態與資料保留
tkctl start cluster demo           # 啟動，等節點回報 Ready（最多約 7 分鐘），列出每個節點的狀態
tkctl delete cluster demo          # 連同狀態目錄與 PVC 資料一併刪除
```

主機重新開機後叢集自動啟動，不需要 `start`。

### 新增與刪除節點

```bash
tkctl add node demo --role worker                        # demo-worker-3
tkctl add node demo --role control-plane --cpu 4         # demo-control-plane-2
tkctl add node demo --role worker --no-join              # 建立並啟動，但不加入叢集
tkctl join node demo demo-worker-4                       # 對它執行 kubeadm join
tkctl delete node demo demo-worker-3                     # drain、停止、移除容器與 volume、自 cluster.yaml 移除
```

新節點的編號是同角色現有最大編號加一。`--defer-join` 建的 control plane 也是用 `join node` 加入。

### image

建叢集需要的全部 image（約 2 GB）由主機下載一次存入共用的 image store，之後每個叢集、每個節點都不再下載。

```bash
tkctl pull images                               # 預先下載，例如上課前；create 會自動做
tkctl create cluster --phase nodes/cni-plugins  # 預先安裝 CNI plugins，不建叢集
tkctl get images                                # 清單與是否已在 store（只看，不拉）
TK_OFFLINE=1 tkctl create cluster               # 離線環境：缺任何 image 就列出並結束，不嘗試下載
```

### 從主機與其他機器連進叢集

主機上 LoadBalancer IP（節點網段的 `.200`–`.219`）與 ClusterIP 直接可達，不需要 port-forward。同一個區網的其他機器加一條到節點網段的路由即可，由主機轉送：

```bash
sudo ip route add 172.22.0.0/24 via <主機 IP>      # Linux；tkctl describe cluster 會印出填好位址的指令
sudo route -n add 172.22.0.0/24 <主機 IP>          # macOS
route add 172.22.0.0 mask 255.255.255.0 <主機 IP>  # Windows
```

## 叢集定義檔

要個別指定節點規格就寫定義檔，旗標在內部也是展開成這種格式（`tkctl describe cluster <名稱> -o yaml` 印的是展開後的 `cluster.yaml`，多了每個節點的位址與 `status`）：

```yaml
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata:
  name: tkdt
spec:
  kubernetes: "1.37.1"
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
      join: false            # 建立並啟動，但不加入叢集（預設 true）
  network:                   # 省略即自動分配
    nodes: 172.22.16.0/24
    router: 172.22.16.253    # router 的位址，預設 .253
    ha: proxy                # 目前只有 proxy
```

```bash
tkctl create cluster -f tkdt.yaml
```

`nodes` 每項是一組同規格節點，`count` 省略是 1，`name` 省略自動編號。`-f` 不能跟 `--control-planes`、`--workers`、`--cpu`、`--memory` 混用。

## 指令

kubectl 風格，動詞在前。全部如下：

```
tkctl create cluster [名稱] [--control-planes N] [--workers N] [--cpu N] [--memory SIZE]
                     [--k8s 版本] [--datapath auto|netkit|veth] [--gvisor] [--ha proxy|bgp] [--defer-join]
                     [--routers 1|2] [--preset lab|ha] [-f cluster.yaml] [--allow-overlap] [--dry-run]
                     [--phase P] [--skip-phases P,...] [--list-phases] [--quiet]
tkctl delete cluster <名稱> [--yes] [--phase P] [--list-phases]
tkctl get clusters
tkctl describe cluster <名稱> [-o yaml]
tkctl stop cluster <名稱>
tkctl start cluster <名稱>
tkctl use cluster <名稱>
tkctl verify cluster <名稱> [--phase P] [--list-phases]
tkctl add node <叢集> --role worker|control-plane [--cpu N] [--memory SIZE] [--no-join]
tkctl join node <叢集> <節點>
tkctl delete node <叢集> <節點> [--yes] [--phase P] [--list-phases]
tkctl pull images [--k8s 版本] [--refresh]
tkctl get images [--k8s 版本]
tkctl delete images --k8s 版本 [--yes]
tkctl version
```

幾個規則：

- 名稱格式 `^[a-z][a-z0-9-]{0,15}$`，用於節點名稱與叢集 DNS 網域（`<名稱>.k8s`）。
- `--control-planes` 要是奇數、最多 5；不論幾台，apiserver 的入口都是 `api.<名稱>.k8s:6443`，由 `.253` 的 VyOS router 服務。`--ha bgp` 時入口不是 router 本身而是 VIP `172.23.N.1`：每台 control plane 的 FRR 只在自己的 apiserver ready 時宣告它，router 對宣告中的 control plane 做 ECMP，整台 control plane 消失靠 BFD 約 1 秒收斂；`describe` 的 `Router:` 段列出 peers 與 next hop 數。
- `--routers 2`：兩台 router `<名稱>-router-1`（.251）與 `-2`（.252）跑 VRRP，節點的閘道與 apiserver 入口仍是 `.253`，在兩台之間漂移（切換約 3 秒）。停一台用 `sudo systemctl stop <名稱>-router-1.service`（`podman stop` 幾秒後會被 systemd 拉回來）；一台停著時 `add node`／`delete node` 只重載跑著的那台，停著的回來時自己拿到新設定。`describe` 的 `Router:` 段列出兩台與 VRRP 狀態，`verify` 的 `[router]` 多一行檢查剛好一台 MASTER、一台 BACKUP。
- `--preset lab|ha` 是拓樸縮寫，`[plan]` 會印出展開的旗標；不能跟 `--control-planes`、`--workers`、`--ha`、`--routers`、`--defer-join`、`-f` 混用。`ha` = `--control-planes 3 --workers 3 --ha bgp --routers 2`，`lab` = 預設的 `--control-planes 1 --workers 2 --ha proxy --routers 1`。
- `--gvisor` 需要 veth datapath，會自動選；明確指定 `--datapath netkit` 加 `--gvisor` 會被拒絕。
- `--memory` 接受 `4G`、`4096M`，`4Gi` 不行。
- 節點的 capacity 就是它的 `cpu`／`memory`，allocatable 是再扣掉 kubeReserved（cpu 500m、memory 512Mi）與 evictionHard（memory 500Mi）：2 核 4G 的節點是 1500m／3084Mi，和一台同規格的 VM 跑 kubeadm 一樣。記憶體至少 2G，任一節點不能超過主機的核數或記憶體（preflight 會拒絕）。`verify` 的 `node sizes` 檢查這些數字。這版之前建的叢集仍回報整台主機，`verify` 對它們印 SKIP；要改過來就重建。
- 已完成的叢集再 `create` 會被拒絕；`delete cluster`、`delete node` 也有 `--phase`，中途失敗再執行一次會接續。
- 環境變數一律 `TK_` 前綴：`TK_DATA_DIR` 變更資料根目錄（預設 `/opt/taroko`）、`TK_ASSUME_YES=1` 等同 `--yes`、`TK_OFFLINE=1` 不下載 image、cilium CLI 與 CNI plugins。`tkctl --help` 有完整清單。
- `PATH` 上任何叫 `tkctl-<名字>` 的執行檔會變成 `tkctl <名字>`（規則同 kubectl 的 plugin）。

## 目錄配置

```
/opt/taroko/clusters/<名稱>/     # cluster.yaml、kubeconfig、storage/（PVC 資料）、logs/
/opt/taroko/images/<K8s 版本>/   # 共用 image store
/opt/taroko/clusters/<名稱>/router/  # VyOS 的設定（tkctl.boot），依 cluster.yaml 產生
/opt/taroko/cni/                 # CNI plugins
/opt/taroko/clusters/<名稱>/nodes/<節點>/  # 節點掛進去的 meminfo、NUMA node 與 kubelet 的保留量設定
/etc/containers/systemd/<名稱>/  # 節點與 router 的 Quadlet unit；systemctl status <名稱>.target
```

## 延伸閱讀

- [架構](docs/architecture.md)：phase 的執行機制、程式碼導讀、網段、狀態目錄、router 與 apiserver 的入口、主機的 sysctl 與 sudo。
- [節點 image 與共用 image store](docs/node-image.md)：節點與 router image 的內容、自行建置、store 為什麼唯讀。
- [開發](docs/development.md)：測試規範、CI、版本政策、與 v1 的差異。

## 想參與

- 發現問題、想提需求 → 開 [issue](https://github.com/tarokolabs/tk8s/issues/new)
- 還不確定要不要做、想討論方向 → 開 [Discussion](https://github.com/tarokolabs/tk8s/discussions)
- **不確定該開哪個 → 開 issue 就好**，維護者會幫你轉

進度看 [Taroko Roadmap](https://github.com/orgs/tarokolabs/projects/1)。

## 授權

本專案採 **GPL-2.0-or-later**（GNU GPL v2，或依你的選擇任何更新版本），見 [LICENSE](LICENSE)。
