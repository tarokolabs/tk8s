# Taroko Kubernetes

在一台或多台 Linux 主機上，以 podman 容器作為節點建立多節點 Kubernetes 叢集。一道指令完成建立，不需要事先修改任何設定檔。

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
| 多主機（`--hosts`） | 每台主機都符合以上條件、在同一個 L2 網段，跑 `tkctl` 的機器能以金鑰 ssh 到每台並 `sudo -n`，ssh 登入的使用者用 POSIX shell（bash、sh）且 rc 檔不印東西（tkctl 讀的是指令輸出）；LAN 上保留一些位址給叢集（`--ip-range`，可以不連續）；主機有 `arping` 探測較快，沒有就用 ping |

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
tkctl use cluster tk8s      # 合進 ~/.kube/config 並切成目前的 context（原檔留在 ~/.kube/config.bak）
kubectl get nodes
```

名稱省略是 `tk8s`，節點是 `tk8s-control-plane-1`、`tk8s-worker-1`、`tk8s-worker-2`，另有一個 VyOS 的 `tk8s-router`：節點的預設閘道，也是 apiserver 的負載平衡。其他常見的拓樸：

```bash
tkctl create cluster demo --workers 3                  # 名稱 demo，三台 worker
tkctl create cluster ha --control-planes 3              # 三台 control plane，HA
tkctl create cluster ha --control-planes 3 --defer-join # 額外的 control plane 建好但不加入，供 HA 練習
tkctl create cluster ha --control-planes 3 --ha bgp     # 同樣三台，apiserver 入口改走 BGP + ECMP 的 anycast VIP
tkctl create cluster ha --mode ha                        # 3 control plane + 3 worker、bgp、兩台 router；[plan] 會印出展開的旗標
tkctl create cluster demo --routers 2                    # 兩台 VyOS router 跑 VRRP，.253 在兩台之間漂移；停一台叢集照常
tkctl create cluster ha --mode ha --hosts a=local,b=tux@192.168.1.231,c=192.168.1.232 --ip-range 192.168.1.64-79,192.168.1.208-223
                                                         # 節點、router 分散到三台主機（macvlan，節點拿 LAN 的位址），任一台主機掛掉叢集照常
tkctl create cluster sandbox --gvisor                   # 多一個 gvisor 的 RuntimeClass
tkctl create cluster old --k8s 1.36.6                   # 次新版
tkctl create cluster big --cpu 4 --memory 8G
tkctl create cluster bare --skip-phases cni,addons    # 只做到 kubeadm，CNI 自己裝（叢集停在 Creating）
```

### 執行輸出

```
[preflight] check the host: podman, systemd, cgroup v2, swap, sudo, sysctls, data tree, node sizes
  preflight ok: podman 5.4.2, systemd, cgroup v2, swap off, host bridge-nf off, ip_forward on, /opt/taroko owned by student, host 8 cpu, 32102 MiB
[plan] resolve cluster.yaml: defaults, subnet, node names
  cluster demo: 1 control plane + 2 workers, K8s 1.37.2, subnet 172.22.0.0/24
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

每個 `[phase]` 一行，其下是結果行；加 `-v`（放哪裡都行，`tkctl -v create …`）會多印每道指令一行 `$ …`，貼回去就能重跑（上面的範例是 `-v` 的輸出；寫入檔案的內容用 `--dry-run` 看）；`already done` 表示這一步先前已完成。中斷後再執行同一個指令，已完成的跳過、未完成的接著做；`tkctl describe cluster demo` 的 `Status:` 列出哪些 phase 何時完成、下一步是什麼。

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
  host-routes        route each node's pod CIDR from the host straight to that node
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
tkctl add node demo --role worker --host c               # 多主機叢集：放到主機 c（省略就放節點最少的那台）
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

主機上 LoadBalancer IP（節點網段的 `.201`–`.219`）、ClusterIP 與 pod IP 直接可達，不需要 port-forward。同一個區網的其他機器加一條到節點網段的路由即可，由主機轉送：

```bash
sudo ip route add 172.22.0.0/24 via <主機 IP>      # Linux；tkctl describe cluster 會印出填好位址的指令
sudo route -n add 172.22.0.0/24 <主機 IP>          # macOS
route add 172.22.0.0 mask 255.255.255.0 <主機 IP>  # Windows
```

多主機叢集的節點與 LoadBalancer IP 本來就在 LAN 上，區網內任何機器直接連得到，不用加路由；ClusterIP 只有操作台連得到（它的 routes unit 經 `tk8s-<名稱>` 這個 shim 介面把路由指進叢集）。

### 多主機

`--hosts` 把節點容器與 router 分散到同一個 L2 的多台主機上，目的是沒有任何一台主機是單點。跑 `tkctl` 的機器是操作台，`cluster.yaml` 與 kubeconfig 在它上面；對其他主機的每一道指令都走 ssh，`-v` 印出來的是 `$ ssh tux@192.168.1.231 'sudo -n podman …'`，一樣可以手動重現。叢集跑起來後不依賴操作台：每台主機的 unit 重開機自己起來。

- 主機清單 `[名稱=][使用者@]主機[:port]` 以逗號分隔；寫 `local`（或 `名稱=local`）的那項是操作台自己，最多一項。ssh 的金鑰、ProxyJump 等由 `~/.ssh/config` 管，host key 要先手動 ssh 一次信任。
- `--ip-range` 是 LAN 上保留給這個叢集的位址池：一段或多段 `A-B` 以逗號分隔，結尾可以只寫最後一組（`192.168.1.64-70,192.168.1.80-89`）。池依固定順序切：第一個是 router 的浮動位址（節點的閘道），`--routers 2` 時接著兩台 router 各一個，操作台若是節點主機再一個 shim，然後每個節點一個，池的最後 6 個：倒數第 6 個保留給 Cluster Mesh（clustermesh-apiserver 的位址），最後 5 個是 LoadBalancer 池，中間剩下的留給 `add node`。preflight 會對整個池 ARP 探測，有人在用就拒絕；池要在主機的網段內（網段、閘道、每台主機的介面由各自的預設路由查出）。
- 放置：control plane 從第一台主機輪流、worker 接著輪、router 從第一台輪；`-f` 裡每個節點群組可寫 `host:` 固定在某台（省略或 `auto` 就輪流），router 用 `network.router_hosts`。三台 control plane 放兩台主機時總有一台持有多數，那台掉了 apiserver 就停，`plan` 與 preflight 會印警告（不拒絕）。
- 節點網路是 Quadlet 的 macvlan network，節點與 router 拿 LAN 的位址、固定的 MAC；router 仍是節點的預設閘道（它扮演 ToR），再轉給 LAN 的閘道。macvlan 讓主機自己連不到它上面的容器，所以操作台若同時是節點主機，會多一個 `tk8s-<名稱>` shim 介面；其他主機不需要。
- `--ha bgp` 跨主機時流量是非對稱的（去程經 router、回程由 control plane 直接回），主機之間的路徑不能有做狀態追蹤的東西：hypervisor 的防火牆（Proxmox 開著 firewall 時要在每台 node 設 `nf_conntrack_allow_invalid: 1`）、stateful switch 都會把回程之後的封包當成不合法丟掉，症狀是 ping 與握手都通、第一個資料封包就卡住。`--ha proxy` 是 haproxy 終結連線，流量對稱，沒有這個前提。
- 每台主機各有一份 `cluster.yaml` 副本（`status.operator` 記著操作台）；在不是操作台的主機上 `get clusters` 會列出它並標 `managed from <操作台>`，其他動詞拒絕。
- `tkctl stop`／`start`、`delete cluster`、`add node`（`--host` 指定主機）、`delete node`、`verify`（多一段 `[hosts]`）都支援多主機；沒有 `cluster.yaml` 的 `delete cluster` 只清操作台這台。
- 不支援：`--gvisor`、`spec.network.nodes`／`router` 明寫、建立後增減主機、不在同一個 L2 的操作台；`tkctl pull images` 一次只對一台主機（create 的 images phase 會對每台做）。

### 聯邦（Cluster Mesh）

`connect` 把同一台操作台管理的幾個叢集接成 Cilium Cluster Mesh：pod 網路互通，同名的 Service 標成 global 後，任一叢集都打得到兩邊的 pod。用途是**藍綠換叢集**取代原地 `kubeadm upgrade`（新版 tk8s 建新叢集、workload 部署過去、入口切過去、舊的刪掉），同一套也給跨叢集的 HA 與分流。

```bash
tkctl connect cluster a b           # 接起來（也可以一次三個以上；接進已有 mesh 的叢集會跟每個成員都接上）
tkctl get clusters                  # MESH 欄：接了誰
tkctl describe cluster a            # Mesh: 段：peers、global service 兩邊的 backend 數、入口位址
tkctl verify cluster a --phase mesh # peers 都在、clustermesh status connected、對面的 pod 打得到
tkctl disconnect cluster a b        # 斷開一對；delete cluster 會自己先斷
```

- 條件（`connect` 會核對，不過就拒絕並說明）：兩個叢集都 Running；都是這版之後建的（Cilium 的 `cluster.name`／`cluster.id` 裝的時候就要設好，之前建的要重建）；同一種形態（都單主機或都多主機，混接不支援：單主機的節點躲在主機的 NAT 後面，LAN 上的節點連不到它們）；Cilium 版本差不超過一個 minor（藍綠升版時舊叢集的 Cilium 本來就可能舊一版）。
- 每個叢集的 LB 池留了一個位址給 clustermesh-apiserver（單主機 `.200`，多主機池尾端倒數第 6 個），`connect` 時才會真的裝上去。
- `connect` 做完會從兩邊探測對面：節點之間 ping，pod 之間以 HTTP 打對面 coredns 的 readiness（pod 沒有 NET_RAW，不能 ping）；探測失敗會印 `FAIL` 並以結束碼 1 收場，但 mesh 已經接上（問題在網路，`disconnect` 可以還原）。
- global service 是 Cilium 原生的，tkctl 不包也不代標：兩邊同 namespace 同 name 的 Service 都加 `service.cilium.io/global: "true"`（一邊沒有 pod 也可以，流量就全去另一邊）；`service.cilium.io/shared: "false"` 讓這邊的 backend 不分享出去；`service.cilium.io/affinity: local|remote|none` 調偏好。`externalTrafficPolicy: Local` 的 Service 從 LoadBalancer 位址或 NodePort 進來只打本節點的 backend，對面永遠不算；叢集內用 ClusterIP 不受影響。
- DNS：每個叢集的 cluster domain 是 `<名稱>.k8s`，pod 用短名（`echo`、`echo.default`、`echo.default.svc`）兩邊都通；寫死完整 FQDN 的 workload 搬叢集要改最後一段。
- 對外入口：HTTP/HTTPS/gRPC 走 Gateway API（一個叢集一個入口位址，換叢集時外面只改一個位址，HTTPRoute 的權重就是逐步切流量的工具）；非 HTTP 走 LoadBalancer Service；前面已有外部負載平衡器時讓它只做 L4，L7 交給叢集裡的 Gateway。Cilium 的 Gateway controller 在 cilium-operator 重啟後可能不回來（上游 cilium/cilium#48083，`verify` 的 `cilium-operator` 檢查會指出），復原是 `kubectl -n kube-system rollout restart deploy/cilium-operator`。
- 單主機之間（同一台主機的兩個叢集）也能接，e2e 就是這樣測的。主機若裝了 Docker，它把 `FORWARD` 預設 DROP，兩個 bridge 之間的 pod 流量會被擋，`connect` 的探測會指出，`sudo iptables -I DOCKER-USER -j ACCEPT` 放行。
- tk8s 不提供外部 LB 或 DNS；lab 裡退掉舊叢集時客戶端要自己改用新叢集的位址。

藍綠升版的一次流程（`a` 舊、`b` 新）：

1. 用新版 tk8s 建 `b`（同一批主機、同一個 LAN、不同 index）。
2. `tkctl connect cluster a b`；`describe cluster a` 的 `Mesh:` 列出 `b`。
3. 同一套 manifest 部署到 `b`（`kubectl --context b apply`、Helm 的另一個 release、Argo CD 多一個 destination），要跨叢集互找的 Service 兩邊都加 global annotation。
4. `describe cluster a` 看每個 global service 兩邊的 backend 數；此時 `a` 的 pod 已經會打到 `b` 的 pod，反之亦然。
5. 外部入口：在外部 LB 把 `b` 的 Gateway（或 LoadBalancer）位址加進 backend（位址抄 `describe cluster b` 的 `entrypoints:`），要逐步切就先給一部分權重，確認後把 `a` 的拿掉；用 DNS 同理，先降 TTL。
6. `a` 的 Deployment 縮到 0 或刪掉；global service 讓 `a` 裡還沒搬的東西繼續找得到 `b` 的服務。
7. `tkctl disconnect cluster a b`、`tkctl delete cluster a`（delete 會自己先 disconnect）。

## 叢集定義檔

要個別指定節點規格就寫定義檔，旗標在內部也是展開成這種格式（`tkctl describe cluster <名稱> -o yaml` 印的是展開後的 `cluster.yaml`，多了每個節點的位址與 `status`）：

```yaml
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata:
  name: tkdt
spec:
  kubernetes: "1.37.2"
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

多主機時多一段 `hosts`，`network` 改寫保留範圍（`nodes`、`router` 由 tkctl 從主機的 LAN 查出與切出，不能寫）：

```yaml
spec:
  hosts:
    - name: pve-a              # 沒有 ssh：操作台自己
    - name: pve-b
      ssh: tux@192.168.1.231   # [使用者@]主機[:port]
    - name: pve-c
      ssh: 192.168.1.232
  network:
    range: 192.168.1.64-70,192.168.1.80-89   # 可多段
    ha: bgp
    routers: 2
    router_hosts: [pve-a, pve-b]   # 省略就輪流
  nodes:
    - role: control-plane
      count: 3                 # 沒寫 host 就輪流：pve-a、pve-b、pve-c
    - role: worker
      count: 2
      host: pve-c              # 固定放這台
```

```bash
tkctl create cluster -f tkdt.yaml
```

`nodes` 每項是一組同規格節點，`count` 省略是 1，`name` 省略自動編號。`-f` 不能跟 `--control-planes`、`--workers`、`--cpu`、`--memory` 混用。

## 指令

kubectl 風格，動詞在前，`-v`／`--verbose` 可以加在任何位置（多印每道 `$` 指令）。全部如下：

```
tkctl create cluster [名稱] [--control-planes N] [--workers N] [--cpu N] [--memory SIZE]
                     [--k8s 版本] [--datapath auto|netkit|veth] [--gvisor] [--ha proxy|bgp] [--defer-join]
                     [--routers 1|2] [--hosts 清單] [--ip-range A-B] [--mode lab|ha] [-f cluster.yaml]
                     [--allow-overlap] [--dry-run] [--phase P] [--skip-phases P,...] [--list-phases]
tkctl delete cluster <名稱> [--yes] [--phase P] [--list-phases]
tkctl get clusters
tkctl describe cluster <名稱> [-o yaml]
tkctl stop cluster <名稱>
tkctl start cluster <名稱>
tkctl use cluster <名稱>
tkctl verify cluster <名稱> [--phase P] [--list-phases]
tkctl connect cluster <a> <b> [<c>...]
tkctl disconnect cluster <a> <b>
tkctl add node <叢集> --role worker|control-plane [--cpu N] [--memory SIZE] [--host 名稱] [--no-join]
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
- `--hosts` 與 `--ip-range` 要一起給，把叢集分散到多台主機（見「多主機」）；`-f` 的對應寫法是 `spec.hosts` 與 `spec.network.range`。
- `connect cluster` 把兩個以上的叢集接成 Cilium Cluster Mesh，`disconnect cluster` 斷開一對（見「聯邦」）；兩邊要同一種形態、Cilium 差不超過一個 minor，這版之前建的叢集要重建才能接。
- `--mode lab|ha` 是拓樸縮寫，`[plan]` 會印出展開的旗標；不能跟 `--control-planes`、`--workers`、`--ha`、`--routers`、`--defer-join`、`-f` 混用。`ha` = `--control-planes 3 --workers 3 --ha bgp --routers 2`，`lab` = 預設的 `--control-planes 1 --workers 2 --ha proxy --routers 1`。
- `--gvisor` 需要 veth datapath，會自動選；明確指定 `--datapath netkit` 加 `--gvisor` 會被拒絕。
- `--memory` 接受 `4G`、`4096M`，`4Gi` 不行。
- 節點的 capacity 就是它的 `cpu`／`memory`，allocatable 是再扣掉 kubeReserved（cpu 500m、memory 512Mi）與 evictionHard（memory 500Mi）：2 核 4G 的節點是 1500m／3084Mi，和一台同規格的 VM 跑 kubeadm 一樣。記憶體至少 2G，任一節點不能超過主機的核數或記憶體（preflight 會拒絕）。`verify` 的 `node sizes` 檢查這些數字。這版之前建的叢集仍回報整台主機，`verify` 對它們印 SKIP；要改過來就重建。
- 每個叢集的 kubeconfig 以叢集命名（cluster `<名稱>`、user `<名稱>-admin`、context `<名稱>`），`use cluster` 把它合進 `~/.kube/config` 並切成目前的 context，其他叢集的項目留著，之後用 `kubectl config use-context <名稱>` 切換；`delete cluster` 會把自己的項目拿掉。`~/.kube/config` 裡不是 kubeconfig 的內容 tkctl 不會動。
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
/etc/systemd/system/<名稱>-routes.service  # 主機到 pod／service 網段的路由；多主機時只有操作台有，兼建 tk8s-<名稱> 這個 macvlan shim
```

多主機時每台主機都有這棵樹，但各放自己節點與 router 的檔；`cluster.yaml` 正本在操作台，其他主機是副本，kubeconfig 只在操作台。

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
