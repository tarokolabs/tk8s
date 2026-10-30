# 架構

tk8s 在一台（或同一個 L2 上的多台）Linux 主機上以 podman 容器作為節點建立 Kubernetes 叢集。這份文件說明它的組成、檔案位置與設計理由；用法見 [README](../README.md)。

## 兩層

| 層 | 負責 | 學員看到什麼 |
|---|---|---|
| `tkctl`（`src/tk8s/`） | 把旗標展開成 `cluster.yaml`，再逐個 phase 執行：網段分配、產生 unit、`kubeadm init` 與 `join`、cilium、addons、日常操作 | `--list-phases` 是流程；執行時每個 phase 一行、每道指令一行 `$ …`，可以逐行重現 |
| Podman Quadlet | 每個節點一個 systemd 服務，一個叢集一個 target；主機重新開機後叢集自動啟動 | `systemctl status <叢集>.target` |

## phase

每個 phase 是 `Phase(name, summary, done, run, children)`：一行說明、`done()` 檢查主機判斷是否已完成、`run()` 執行。`Runner` 依序走過一棵樹，`done()` 為真就印 `already done` 跳過，所以中斷後再執行同一個指令，已完成的跳過、未完成的接著做。`create cluster` 完成的 phase 連同時間記入 `cluster.yaml` 的 `status`，`tkctl describe cluster` 的 `Status:` 列出來並指出下一步。

- `--phase P` 只執行一個 phase 或一棵子樹（`kubeadm`、`kubeadm/init`，只寫 `init` 也行）；`--skip-phases a,b` 跳過幾個。
- `--dry-run` 換成一個只印不做的 host，只執行 plan、units 與 kubeadm/render：印出解析後的 `cluster.yaml` 與會寫入主機的每個檔。
- `delete cluster`、`delete node` 的 phase 也是同一套，只是 `done()` 的意思是「該資源已不存在」；`get`、`describe`、`verify` 不寫 `status`。
- `add node` 沒有 `--phase`：它的 phase 是 create 的子集加 join，新增中斷後用 `tkctl join node` 完成後續步驟。
- 有跳過任何 phase 的叢集不標 Ready：`get clusters` 顯示 Creating，節點動詞會拒絕，再執行同一個 `create` 把跳過的做完才算完成。`--skip-phases cni,addons` 因此可以當「只到 kubeadm、CNI 自己裝」的練習環境，裝好 CNI 後叢集仍停在 Creating，這是刻意的。

步驟的切法仿照 kubeadm 的 phase：preflight、plan、images、units、nodes、kubeadm（render、init、kubeconfig、join）、cni、addons。

## 程式碼怎麼讀

```
src/tk8s/
  cli.py args.py verbs.py   # 參數解析、旗標驗證、每個動詞一個 cmd_*
  phases.py                 # Phase、Runner、Context
  host.py                   # 執行指令與寫檔的唯一入口；DryRunHost 只印、FakeHost 給測試
  podman.py                 # 問 podman 物件在不在、在節點容器裡跑指令：全 repo 只有這裡拼這些 argv
  model.py                  # cluster.yaml 的型別（pydantic，frozen，extra=forbid）
  paths.py versions.py      # 路徑與 versions.yaml
  create/                   # create cluster 的 phase，一個 phase 一檔，__init__ 排順序；
                            # router.py 跟 router 說話、router_config.py 產 tkctl.boot 的區塊、router_state.py 讀容器與 VRRP 狀態
  node/                     # add.py、delete.py 一個動詞一檔，common.py 是兩者共用的函式
  lifecycle.py              # get、describe、stop、start、use
  describe_router.py        # describe 的 Router: 段（一台或兩台 router、VRRP 狀態）
  delete.py                 # delete cluster
  images.py                 # 共用 image store
  verify/                   # verify cluster 的 15 個檢查，一個檢查一個函式，一個 [section] 一檔
templates/                  # 寫到主機上的檔，與實際內容只差 $變數
```

`create/` 的 phase 樹最大（八個頂層、十二個子 phase），一個檔案放不下，所以是一個目錄：`src/tk8s/create/<phase>.py` 從上讀到下就是那個 phase 的步驟，一個函式一步。`node/` 用同一個理由：add 與 join 重用 `create/` 的 units、kubeadm、nodes、addons 對單一節點重做建叢集的步驟，delete node 重用 `delete.py` 移除容器、volume 與 unit。phase 是函式與 tuple，沒有 plugin 註冊、沒有 dispatch table；唯一的 class 階層是 `Host` 與它的 dry-run、測試替身。

## 網段

網段自動分配，每個叢集分配一個索引 N，彼此不重疊：

| 網段 | 範圍 | 固定位址 |
|---|---|---|
| 節點 | `172.22.N.0/24` | router `.253`（節點的預設閘道，`spec.network.router` 可改）、主機 bridge `.254`（router 的上游）、Cluster Mesh 的 apiserver `.200`、LoadBalancer 池 `.201`–`.219` |
| pod | `10.244.(8N).0/21` | |
| service | `10.98.N.0/24` | |

要自行指定就寫在 `spec.network`；與既有叢集重疊會被拒絕，`--allow-overlap` 允許。

節點的預設路由指向 router，不是主機的 bridge：`.network` 設 `no_default_route=1` 並以 `--route 0.0.0.0/0,<router>` 發給每個容器。router 自己也會拿到那條指向自己的路由，而且 zebra 把它當 kernel route 壓過 VyOS 的 static default，所以 `router/scripts/vyos-postconfig-bootup.script` 開機時把它刪掉，之後 zebra 才裝上指向 `.254` 的那條；容器停止時 netavark 會再刪一次同一條路由，找不到就讓 unit 以失敗收場，所以同一個腳本登記一個 transient unit 在關機時把它放回去；同一個腳本也關掉 router 的 ICMP redirect（主機 bridge 和節點在同一條 link 上，否則 router 轉送每個封包都會叫節點直接走 `.254`）。

## 狀態目錄與 unit

```
/opt/taroko/clusters/<名稱>/     # cluster.yaml、kubeconfig、init-config.yaml、storage/、logs/
/opt/taroko/clusters/<名稱>/router/  # tkctl.boot 與兩個開機腳本，依 cluster.yaml 產生；VyOS 自己寫的 config.boot 與 archive 也在這
/opt/taroko/clusters/<名稱>/router-1/、router-2/  # --routers 2 時取代 router/，一台一個目錄
/opt/taroko/clusters/<名稱>/nodes/<節點>/  # 節點掛進去的 meminfo、sys-node/（假的 NUMA node）、kubelet-reserved.conf 與 kubelet-config-dir.conf
/opt/taroko/cni/                 # CNI plugins，所有叢集共用
/opt/taroko/images/<K8s 版本>/   # 共用 image store，節點唯讀掛載；<版本>.conf 是它的 storage.conf
/etc/containers/systemd/<名稱>/  # Quadlet unit：<名稱>.network、<名稱>-router.container（--routers 2 時 -router-1 與 -router-2）、每個節點一個 .container
/etc/systemd/system/<名稱>.target
/etc/systemd/system/<名稱>-routes.service   # 主機到 pod 與 service 網段的路由，跟 target 一起啟停
```

`TK_DATA_DIR` 換掉 `/opt/taroko`。第一次建立 `/opt/taroko` 需要 sudo，之後歸使用者擁有。

節點容器由 Quadlet 管理，每次啟停會重建；節點必須保留的 `/var`、`/etc`、`/usr/local/bin` 放在每節點的 named volume（`<節點>-var`、`-etc`、`-usr-local-bin`），`delete` 會一併移除。

`cluster.yaml` 的 `status` 區塊（`phase: Creating|Ready`、`completed:` 每個 phase 的完成時間、`tk8s:` 建立時的 tk8s 版本）由 tkctl 寫，`-f` 的檔不能有。沒有 `status` 的 `cluster.yaml`（v2026.10.7 之前建的叢集）tkctl 不再讀 `.create-complete` 標記來判 Ready：`get clusters` 列為 Creating，`create` 拒絕把它當成中斷的建立來續跑（續跑會依今天的旗標重算拓樸並覆寫檔案）；請刪除後重建。

## 節點看到的規格

`--cpus`／`--memory` 只會進 podman 的 cgroup，kubelet 不看 cgroup：它內建的 cAdvisor 從 `/proc/meminfo` 的 `MemTotal` 算記憶體、數 `/sys/devices/system/node/node*/cpu[0-9]*` 有幾個目錄算 CPU，兩條路在容器裡看到的都是主機。少了修正，節點會回報整台主機的 capacity，排程器照那個數字塞 pod，kubepods.slice 的 memory.max 被設成主機值，pod 加總撞到的是容器 cgroup 的 kernel OOM，`evictionHard` 以主機容量計算永遠不會先出手。上游沒有要修（cadvisor PR #2699 被拒、kubernetes #120832 not-planned、kind #877 凍結）。

tkctl 用四個每節點一份、依 cluster.yaml 產生的檔案處理，放在 `nodes/<節點>/`，由節點的 Quadlet unit bind mount 進去：

- `meminfo` 蓋在 `/proc/meminfo` 上，只有 `MemTotal`（節點的記憶體）與 `SwapTotal: 0` 兩行，cAdvisor 只讀這兩行。capacity 的記憶體因此等於 `memory`。
- `sys-node/` 蓋在 `/sys/devices/system/node` 上，裡面只有 `node0`，`node0/cpu0`…`cpu<N-1>` 是指向真的 `/sys/devices/system/cpu/cpuN` 的 symlink（topology、cache 都是真資料，只有數量變成 N），加上 `cpulist`、`cpumap`（`lscpu` 要）、`distance`、`meminfo` 與 hugepages 幾個小檔。capacity 的 CPU 因此等於 `cpu`。節點 image 裡只有 kubelet 讀這個目錄。
- `kubelet-reserved.conf` 掛到 `/etc/kubernetes/kubelet.conf.d/`，kubelet 用 `--config-dir` 把它疊在 kubeadm 產生的 `/var/lib/kubelet/config.yaml` 之上；`--config-dir` 這個旗標來自同樣掛進去的 systemd drop-in `kubelet-config-dir.conf`（`/etc/systemd/system/kubelet.service.d/20-tk8s-config-dir.conf`），不放在 image 裡：放 image 的話，舊叢集上 `add node` 的新節點會拿到指向不存在目錄的旗標而起不來，而且 `node:v<版本>` 這個 tag 會浮動，主機上是哪一版 image 無從得知。內容只有 `kubeReserved: cpu 500m、memory 512Mi`。kubelet 自己把 kubepods.slice 的 memory.max 設成 capacity − 512Mi，並把 `evictionHard` 的 `memory.available: 500Mi` 複製成看 kubepods cgroup 的 `allocatableMemory.available` 訊號，所以 pod 吃到快滿時先被驅逐，不等 kernel OOM。沒有 systemReserved：capacity 已經是節點規格，再扣「主機減節點」會讓保留量大於 capacity，kubelet 拒絕啟動。

結果就是 `kubectl describe node` 長得和一台同規格的 VM 一樣：capacity 2／4Gi、allocatable 1500m／3084Mi。`verify` 的 `node sizes` 同時比 capacity 與 allocatable：哪一層沒掛上（例如有人手改了 unit）都會露餡。

幾個看得到的現象：節點內 `nproc` 與 `/proc/cpuinfo` 仍是主機核數（那些路徑太多東西在讀，不蓋）；`free` 的 total 是節點規格、used 等於 total、available 0（假檔只有 MemTotal），看資源用 `kubectl top node` 或 cgroup 檔；eviction manager 每 10 秒看一次，pod 每秒長好幾百 MiB 時 500Mi 的門檻幾秒就被衝過，仍是 kernel OOM 殺 pod。

這版之前建的叢集（cluster.yaml 的 `status` 沒有 `node_capacity: true`）完全不受影響：units phase 對它們產生的 unit 和以前一樣，`add node`、`delete node`、`start` 都不會動它們，`verify` 的 `node sizes` 印 SKIP。要新行為就重建。

## router 與 apiserver 的入口

每個叢集有一個 VyOS 容器跟節點一起啟停：`<名稱>-router`，位址 `.253`。它是節點的預設閘道（連外流量經它轉給主機 bridge `.254`），也是 apiserver 的負載平衡：`load-balancing haproxy` 在 6443 接連線，對 `cluster.yaml` 裡的每一台 control plane 做 TCP 檢查、round-robin、TLS passthrough。`controlPlaneEndpoint` 與憑證都是 `api.<名稱>.k8s:6443`：每個節點把這個名字指到 `.253`（kubelet、Cilium 都走它），主機的 kubeconfig 直接寫 `.253`。這是 kubeadm 文件所畫的 HA 拓樸：一個 load balancer 在 control plane 前面，所有客戶端都連它。

tkctl 渲染的是 `tkctl.boot`；VyOS 開機前的 `vyos-preconfig-bootup.script` 把它複製成 `config.boot` 並補上 VyOS 自己產生的版本註腳，沒有這行 VyOS 會對檔案跑一輪 migration（加區塊、改寫檔案）。新增或刪除 control plane 時 tkctl 重新產生 `tkctl.boot`，以 `vbash` 做同一件事再 `load`、`commit`，VyOS 套用差異，既有連線不中斷（也因此 backend 轉 DOWN 時既有連線不會像 HAProxy 的 `shutdown-sessions` 那樣被切斷，要等 client 自己發現）；VyOS 的 `commit` 結束碼不可靠（沒有變更回 1、失敗反而回 0），所以 tkctl 等到 `show haproxy` 列出的 backend 正好是 cluster.yaml 的 control plane 才算完成，node add/delete 重跑時也以此判斷 router 是否已更新。刪除 control plane 時，`kubeadm reset` 讓那台的 apiserver 消失後 router 還會把新連線送過去幾秒（haproxy 的 check 約 6 秒才標 DOWN；bgp 模式要等 health timer 撤回），所以 `delete node` 先等 router 不再列它（`show haproxy` 的 backend 離開 UP，或 VIP 路由的 next hop 少了它），再 `kubectl delete node`。`sudo podman exec -it <名稱>-router vbash` 進去就是 VyOS 的 CLI：`show configuration`、`show haproxy`、`show ip route`。

**為什麼是 TCP 檢查**：VyOS 的 haproxy 整合沒有 `check-ssl`，設 `ssl` 會把流量也包進 TLS、破壞 passthrough，`http-check` 又只能明文，所以打 `/readyz` 的健康檢查做不到。應用層的健康檢查本來就是負載平衡器而不是 router 的功能，雲端給 apiserver 的 NLB 也是 TCP 檢查。代價是 apiserver 起來但還沒 ready 的那幾秒流量仍會送過去，client 的重試會蓋掉；CP 停掉約 6 秒才標 DOWN。

跟 VIP 的差別：沒有 ARP、沒有 lease、沒有 static pod；單 control plane 的叢集也走同一條路，所以 `verify` 的 `[router]` 每個叢集都會檢查。pod 內不需要也無法解析 `api.<名稱>.k8s`（叢集內的 CoreDNS 擁有這個網域）：pod 走 `kubernetes` Service 的 ClusterIP。

### bgp 模式（`--ha bgp`）

同一個 VyOS 容器，換一份 `tkctl.boot`：沒有 haproxy，`protocols bgp`（AS 65000，每台 control plane 一個 eBGP neighbor，AS 65001，`maximum-paths ebgp 8`）加 `protocols bfd`（300 ms × 3）。`api.<名稱>.k8s` 解析到 VIP `172.23.N.1`，刻意放在另一個 /16：在節點網段裡的位址節點會直接 ARP，就看不到路由。主機的 `<名稱>-routes.service` 多一條 `VIP/32 via .253`，kubeconfig 直接寫 VIP。

每台 control plane 裡跑 node image 內建的 FRR（`kubeadm/frr` 寫 `/etc/frr/frr.conf`、`/etc/tk8s/vip`，啟用 `frr.service` 與 `tk8s-apiserver-health.timer`）。`/etc/frr/frr.conf` 由 tkctl 從 `templates/node/frr.conf` 算出來，每次 `kubeadm/frr` 都比對整份內容、不同就重寫，所以在節點裡手改會在下一次 create、add node 或 join node 時被蓋回去。timer 每秒打本機 `/readyz`：連續 3 次 ok 才把 VIP 放到 `lo`，連續 2 次不是 ok 才拿掉，一次慢回應或 refused 不動它（每次變動都會讓 router 的 ECMP 重算 hash）；探測失敗、撤回、宣告都寫進節點的 journal（`journalctl -u tk8s-apiserver-health`），級距夠不夠寬看那裡；FRR 的 `redistribute connected route-map vip-only` 加 `neighbor … route-map vip-only out` 只宣告那個 /32，所以宣告與撤回就是 `ip addr show lo` 的一個位址。router 對所有宣告中的 control plane 做 ECMP（`show ip route 172.23.N.1`），每台 control plane 連 VIP 連到的是自己（lo 的本機路由優先，anycast 的正常行為）。

兩種故障、兩種收斂：apiserver 掛但節點活著，timer 連續兩次探測失敗後撤回，約 2 秒（探測掛住不回應時約 3 秒）；整台 control plane 無聲消失，BFD 約 1 秒判定鄰居死亡（只靠 BGP timers 3/9 要 9 秒）；`podman stop` 這種優雅停止 FRR 送 cease，0.3 秒撤回。憑證 SAN 同時含 `.253` 與 VIP。`verify` 的 `[router]` 在這個模式檢查每台已加入的 control plane Established、ECMP 的 next hop 數（也就是正在宣告 VIP 的 control plane 數）等於已加入的 control plane 數、BFD 全 up。router 另有一條 `reject` 的 static route 指向 VIP（distance 254，BGP 的 20 會壓過它）：沒有任何 control plane 宣告時，client 立刻收到 unreachable，而不是封包在 router 與主機之間繞到 TTL 耗盡。

一個 ECMP 固有的代價：control plane 加入、離開或撤回時 next hop 群組重算 hash，既有連到 VIP 的連線有一部分會被送到另一台、收到 RST 重連；kubectl 與各元件的 client 會自己重連，但 bgp 模式不像 proxy 模式那樣保證既有連線不受影響。

### 兩台 router（`--routers 2`）

`spec.network.routers: 2`（旗標 `--routers 2`，`--preset ha` 也是）把 router 變成兩台：`<名稱>-router-1`（`.251`，VRRP priority 200）與 `<名稱>-router-2`（`.252`，priority 100），各自一個 Quadlet 與狀態目錄（`router-1/`、`router-2/`），同一份 `tkctl.boot` 樣板只差自己的位址、host-name 與 VRRP 區塊。`.253` 不再是哪一台的位址，而是 VyOS `high-availability vrrp group tk8s` 的浮動位址（vrid 10、advertise-interval 用預設 1 秒、multicast），所以節點的閘道、`api.<名稱>.k8s` 的解析、主機到 VIP 的路由在一台或兩台時完全相同；`cluster.yaml` 一律寫 `routers:`（單 router 是 `routers: 1`）。

兩種模式都支援。proxy：兩台各自一份完整的 haproxy 設定（server 清單相同，綁 0.0.0.0:6443），誰持有 `.253` 誰在服務，切換時既有連線斷一次、client 重連。bgp：每台 control plane 的 `frr.conf` 對兩台 router 各一個 neighbor 與 BFD peer，兩台各自學到 VIP 的 ECMP，router 之間不需要 iBGP；主機的 `VIP/32 via .253` 走浮動位址，所以 VIP 的可用性也受 VRRP 的切換時間限制。

**切換時間**（VM 3000 實測，100 ms 的 ping）：`systemctl stop` 是優雅關機，keepalived 離開前會宣告讓位，BACKUP 立刻接手：host→VIP 0 掉包、節點→外網掉 2 個。router 被強殺（`podman stop -t 0`、程序崩潰）時沒有這個宣告，BACKUP 要等 3 次沒收到心跳才接手，約 3.3 秒（各掉 33 個）；VyOS 的 `advertise-interval` 只收整數秒（1–255），1 秒 × 3 就是這個地板。對比之下 bgp 模式的 VIP 撤回在 1 秒內。preempt 開著：router-1 回來就搶回 MASTER，但帶 `preempt-delay 10`，因為剛開機的 router-1 若立刻拿回 `.253`，它的 BGP 還要幾秒才學到 VIP，這段時間 host→VIP 只會撞到 `reject` 那條；延遲讓 BGP 先收斂再接手。實測 `systemctl start` 後 VyOS 約 17 秒開完、先是 BACKUP，再過十幾秒成為 MASTER（交接那一拍兩台會短暫都顯示 BACKUP），從 start 起 60 秒 host→VIP 0 掉包；停掉 BACKUP 那台什麼都不會發生。其他數字：`--preset ha`（8 個容器）建叢集 3 分 31 秒，add control plane 71 秒、delete 24 秒（兩台 router 都重載），`tkctl stop`／`start` 回來 33 秒。

**停一台**：Quadlet 的 unit 有 `Restart=always`，`podman stop <名稱>-router-1` 幾秒後 systemd 就把它拉回來；要讓它停著是 `sudo systemctl stop <名稱>-router-1.service`，回來是 `systemctl start`（`describe` 的 `stop:` 行寫的就是這個）。一台停著時 `add node`／`delete node` 只重載跑著的那台（`podman container inspect` 說不在跑的跳過，並印一行說它開機時會拿到新的 `tkctl.boot`：開機腳本每次都把 `tkctl.boot` 蓋成 `config.boot`）；兩台都不在跑才失敗。delete control plane 等每一台跑著的 router 都不再送連線給它。`verify` 的 `[router]` 每台一行，另加一行 VRRP：剛好一台 MASTER、一台 BACKUP，誰是 MASTER 不管；停著的那台就是 FAIL。create 的 `nodes/up` 與 `kubeadm/init` 仍等兩台都好。

## 多主機

`spec.hosts`（旗標 `--hosts`）存在的叢集把節點容器與 router 分散到多台主機上。叢集本身不變：同樣的 node image、Quadlet、kubeadm、Cilium、VyOS；變的是「每個容器在哪台主機」和「節點怎麼接上網路」。

**操作台與 ssh。** 跑 `tkctl` 的機器是操作台：`cluster.yaml` 正本、kubeconfig、`logs/`、`.certificate-key` 在它上面，`kubectl` 與 `cilium` CLI 在它上面跑。對其他主機的每一道指令都是一次 `ssh`（`host.py` 的 `SshHost`，跟本機的 `Host` 同一組方法：`run` 是 `ssh <目標> '<指令>'`，`write` 是 `install -D -m <mode> /dev/stdin <路徑>` 從 stdin 餵內容，`read` 是 `cat`），印出來的 `$` 行就是可以貼回終端的 ssh 形式。ssh 固定 `BatchMode=yes`（host key 未信任或金鑰被拒立刻以 255 失敗，不掛住）與 `ControlMaster=auto`／`ControlPersist=60`（一次 create 對一台主機要跑上百道指令，共用一條連線），socket 放 `$XDG_RUNTIME_DIR/tk8s/`（沒有就暫存目錄下的 `tk8s-<uid>`，必須是自己擁有、別人進不去的真目錄）。`Context.hosts` 是名稱到 Host 的對照，`ctx.host_for(容器名)` 回它所在的主機；對容器做事的程式碼（`podman.exec_`、unit、`systemctl start`）用它，對主機本身做事的 phase（preflight、images、cni-plugins、nodes/up、delete 的大半）對每台各跑一次（`ctx.on(host)`）。叢集跑起來後不依賴操作台：每台主機的 target 重開機自己起來；操作台掛了，把 `clusters/<名稱>/` 搬到別台一樣能管。操作台必須在同一個 L2（preflight 檢查），不能是 macOS。

**網路：macvlan。** 單主機的 bridge 換成 Quadlet 的 `Driver=macvlan`，`parent` 是該主機預設路由出去的介面（每台各自，plan 時從每台的 `ip -j route show default` 與該介面的位址查出，網段與閘道必須一致，記進 `cluster.yaml`，preflight 每次比對）；節點、router 拿 LAN 的位址，跟 LAN 上的一台 VM 沒兩樣。位址來自使用者保留的位址池 `spec.network.range`（一段或多段 `A-B`，逗號分隔；結尾可只寫最後一組，存檔時寫成完整形式），依固定順序切：第 1 個是 router 的浮動位址（`network.router`：節點的閘道、proxy 模式的 apiserver 入口、兩台 router 時的 VRRP 位址），`routers: 2` 時第 2、3 個是 router-1、router-2，操作台若也是節點主機再一個 shim（`network.shim`），接著節點依展開順序，池的最後 6 個：倒數第 6 個保留給 Cluster Mesh 的 apiserver，最後 5 個是 LoadBalancer 池（Cilium 的 pool 一段一個 block），中間剩下的給 `add node`。pod、service 網段與 bgp 的 VIP 仍用叢集索引 N（`10.244.(8N).0/21`、`10.98.N.0/24`、`172.23.N.1`），索引以操作台上看得到的叢集決定（副本也算），跟單主機一樣可用 `spec.network` 明寫。每個容器的 MAC 由 IP 算出（`02:54:4b:` 加 IP 後三個 byte）：netavark 預設每次建容器都換 MAC，Quadlet 每次啟動都重建容器，固定 IP 換 MAC 會讓 LAN 上的 ARP cache 指到舊 MAC 直到過期。Cilium 的 native routing 加 `autoDirectNodeRoutes` 與 LoadBalancer 的 L2 announcement 都要求節點在同一個 L2，macvlan 剛好滿足；LoadBalancer 的 IP 因此 LAN 上任何機器都連得到。router 仍是節點的預設閘道（它在這個架構裡扮演 ToR：閘道、haproxy 或 BGP 鄰居都是它），再轉給 LAN 的真閘道；bgp 模式的 VIP 路由因此不用改，主機的 `routes.service` 仍是 `VIP/32 via <router 浮動位址>`。

**主機 shim。** macvlan 的設計是 parent 介面與它的子介面互不相通：主機自己連不到它上面的節點與 router。需要從主機網路連叢集的只有操作台（kubectl、`/readyz`、verify 的 curl），所以只有「操作台同時是節點主機」時它的 `<名稱>-routes.service`（樣板 `routes-macvlan.service`）在今天的路由之前多建一個 macvlan 子介面 `tk8s-<名稱>`（叢集名稱超過 10 字取前 7 字加兩位 hash，塞進核心的 15 字上限），掛 `/32` 的 shim 位址（不是 LAN 的 prefix：parent 已經有一條 LAN 的 connected route，再掛一條會互相競爭），到 `range` 的路由 `dev tk8s-<名稱> src <shim 位址>`，pod、service、VIP 的路由也都從這個介面出去。其他節點主機沒有 routes unit（tkctl 在它們上面只做 `podman exec`、`systemctl`、寫檔，不經網路）。用 systemd oneshot 而不是主機的網路服務（ifupdown、netplan、NetworkManager、networkd）：主機的網路服務不只一種、shim 的生命週期要跟叢集的 target 走、不碰主機管理員的設定檔。NetworkManager 與 systemd-networkd 會看到新介面，preflight 用它們各自的標準機制排除 `tk8s-*`：`/etc/NetworkManager/conf.d/90-tk8s.conf`（`unmanaged-devices=interface-name:tk8s-*`）與 `/etc/systemd/network/90-tk8s-unmanaged.network`（`Unmanaged=yes`），跟寫 sysctl drop-in 同一類事。操作台不是節點主機時不需要 shim：它到 router 浮動位址走 LAN，只要一份今天的 `routes.service`（pod、service、VIP 三條路由）和一個只 Wants 它的 target，`nodes/up`、`start`、`stop`、`delete` 都把它當一台主機處理。pod／service 的路由跟單主機一樣經 control-plane-1，這是主機上的便利路徑（verify、使用者 curl ClusterIP），不是叢集運作的一部分。

**每台主機的檔案。** `clusters/<名稱>/` 在每台主機都存在，但各放自己需要的：`init-config.yaml` 與 audit policy 在 control-plane-1 所在主機（kubeadm init 從節點裡讀），`router/`、`router-1/`、`router-2/` 在該台 router 所在主機，`nodes/<節點>/` 在節點所在主機，`storage/` 每台（local-path 的 PV 是節點本地的），`cluster.yaml` 每台一份副本（`status.operator` 記操作台的 hostname；操作台每次改動後重新複製）。副本的用途：主機上的叢集索引分配、image store 的 prune（`tkctl delete images` 把它算進使用者）、名稱衝突檢查都是掃 `clusters/*/cluster.yaml` 決定的；在不是操作台的主機上 `get clusters` 列出它並標 `managed from <操作台>`，其他動詞拒絕。`/etc/containers/systemd/<名稱>/` 每台只有自己的 `.network`（parent 不同）、節點與 router 的 `.container`；`<名稱>.target` 只 Wants 自己這台的 unit；image store 與 CNI plugins 每台各自 pull／下載（image 清單在操作台算一次，因為它需要 node image 與 cilium CLI）。

**續跑與副本的保護。** 中斷後再跑同一個 create 時，主機清單（名稱與 ssh）、每台的介面、放置、位址池都必須跟 `cluster.yaml` 一樣，否則拒絕（已寫到某台主機的 unit 不會被收回）。`status.operator` 是操作台的 hostname，preflight 拒絕兩台主機同名（範本 clone 出來的 VM 常這樣）。ssh 本身失敗（exit 255：沒路由、host key、金鑰）一律是錯誤，不會被當成「那台上面沒有東西」，所以某台主機斷線時 `delete cluster` 會停下來而不是跳過它。

**放置與多數。** control plane 從第一台主機輪流、worker 接著輪、router 從第一台輪，寫了 `host:`（或 `router_hosts`）的固定不佔輪流位置；`add node` 沒指定 `--host` 放節點最少的主機。control plane 數不超過主機數時每台最多一台，任一台主機掉了叢集照常；主機比 control plane 少時總有一台持有多數，那台掉了 etcd 失去 quorum、apiserver 停，plan 與 preflight 印警告而不拒絕（兩台主機仍比一台好）。`datapath: auto` 取所有主機最低的 kernel 決定（有一台低於 6.8 就全部 veth）；明寫 `netkit` 而某台太舊，preflight 拒絕並指名。

**bgp 模式的非對稱路徑。** anycast VIP 的流量本來就是非對稱：節點送往 VIP 的封包經 router 的 ECMP 到某台 control plane，回覆從那台 control plane 直接回到節點，不經 router。單主機時一切在一台主機的 bridge 裡，沒有人在乎；跨主機時去程與回程走的是不同主機之間的線路，中間任何做 TCP 狀態追蹤的東西都會壞：lab 的 Proxmox 開著防火牆（`bridge-nf-call-iptables=1`、`PVEFW-FORWARD` 的 `INVALID -j DROP`），router 所在的 hypervisor 只看到 SYN、沒看到從別台回去的 SYN-ACK，之後的 ACK 與資料全被丟掉，症狀是 ping 與握手都通、TLS 的第一個資料封包就卡在 FIN-WAIT-1；而且因為 ECMP 以來源位址 hash，剛好分到 router 同一台主機上的 control plane 的節點不會遇到。解法在環境端：Proxmox 每台 node 設 host 防火牆選項 `nf_conntrack_allow_invalid: 1`（或關掉防火牆）；proxy 模式沒有這個前提（haproxy 終結連線，流量對稱）。

**主機的核心參數。** shim 的來源位址是池裡的 `/32`，封包從 shim 出去、回覆從 parent 介面進來：主機若把 `rp_filter` 設成 1（RHEL 系的預設；Debian 是 0 或 2），回覆會被當成來源不對而丟掉，preflight 沒有檢查這個，症狀是操作台 ping 得到容器卻收不到回覆，把該介面的 `net.ipv4.conf.<iface>.rp_filter` 設成 2 即可。`arp_ignore=0`（預設）下，同一台主機上 parent 與 shim 都會回答對方位址的 ARP（ARP flux），對 macvlan 沒有影響，但 `ip neigh` 看到 shim 位址對應 parent 的 MAC 不要意外。

**看得到的現象與邊界。** 主機的 `eth0` 位址連不到自己這台的容器（操作台經 shim），容器也連不到主機的 `eth0` 位址；`lscpu`／`nproc` 等節點內的現象同單主機。`verify` 多一段 `[hosts]`（每台 ssh 可達、target active、shim 存在），單主機不印。這版不支援：`--gvisor`（tarball 要送到每台）、`spec.network.nodes`／`router` 明寫、建立後增減主機（`spec.hosts` 固定；要加主機就重建）、不在同一個 L2 的操作台、把操作台搬到另一台**節點**主機（它需要 shim；搬到非節點的機器只要改 `status.operator` 並補上原操作台的 `ssh`）、`tkctl pull images --hosts`（在每台主機各跑，或讓 create 的 images phase 做）。沒有 `cluster.yaml` 的 `delete cluster` 只認得操作台這台，其他主機要各自跑一次。

## 主機的 sysctl

前置檢查會把主機的 `net.bridge.bridge-nf-call-iptables` 設為 0 並寫入 `/etc/sysctl.d/90-tk8s.conf`：節點之間的橋接流量不經主機 iptables，否則裝了 Docker 的主機（FORWARD 預設 DROP）會擋掉 pod 到其他節點的流量。`net.ipv4.ip_forward` 必須是 1：節點的網路命名空間建立時複製主機的這個值，未啟用時 kubeadm 的 preflight 會在節點裡失敗。

## sudo 用在哪

只用在必要處：`podman` 與 `systemctl`（節點是系統層級的 Quadlet unit）、寫 `/etc` 的 unit 與 sysctl 檔、寫 `/usr/local/bin`、第一次建立 `/opt/taroko`、刪叢集時移除節點以 root 寫下的資料。每一道都以 `$ sudo -n …` 印出。
