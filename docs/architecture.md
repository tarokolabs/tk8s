# 架構

tk8s 在單一 Linux 主機上以 podman 容器作為節點建立 Kubernetes 叢集。這份文件說明它的組成、檔案位置與設計理由；用法見 [README](../README.md)。

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
  model.py                  # cluster.yaml 的型別（pydantic，frozen，extra=forbid）
  paths.py versions.py      # 路徑與 versions.yaml
  create/                   # create cluster 的 phase，一個 phase 一檔，__init__ 排順序
  node/                     # add.py、delete.py 一個動詞一檔，common.py 是兩者共用的函式
  lifecycle.py              # get、describe、stop、start、use
  delete.py                 # delete cluster
  images.py                 # 共用 image store
  verify.py                 # verify cluster 的 15 個檢查，一個檢查一個函式
templates/                  # 寫到主機上的檔，與實際內容只差 $變數
```

`create/` 的 phase 樹最大（八個頂層、十二個子 phase），一個檔案放不下，所以是一個目錄：`src/tk8s/create/<phase>.py` 從上讀到下就是那個 phase 的步驟，一個函式一步。`node/` 用同一個理由：add 與 join 重用 `create/` 的 units、kubeadm、nodes、addons 對單一節點重做建叢集的步驟，delete node 重用 `delete.py` 移除容器、volume 與 unit。phase 是函式與 tuple，沒有 plugin 註冊、沒有 dispatch table；唯一的 class 階層是 `Host` 與它的 dry-run、測試替身。

## 網段

網段自動分配，每個叢集分配一個索引 N，彼此不重疊：

| 網段 | 範圍 | 固定位址 |
|---|---|---|
| 節點 | `172.22.N.0/24` | router `.253`（節點的預設閘道，`spec.network.router` 可改）、主機 bridge `.254`（router 的上游）、LoadBalancer 池 `.200`–`.219` |
| pod | `10.244.(8N).0/21` | |
| service | `10.98.N.0/24` | |

要自行指定就寫在 `spec.network`；與既有叢集重疊會被拒絕，`--allow-overlap` 允許。

節點的預設路由指向 router，不是主機的 bridge：`.network` 設 `no_default_route=1` 並以 `--route 0.0.0.0/0,<router>` 發給每個容器。router 自己也會拿到那條指向自己的路由，而且 zebra 把它當 kernel route 壓過 VyOS 的 static default，所以 `router/scripts/vyos-postconfig-bootup.script` 開機時把它刪掉，之後 zebra 才裝上指向 `.254` 的那條。

## 狀態目錄與 unit

```
/opt/taroko/clusters/<名稱>/     # cluster.yaml、kubeconfig、init-config.yaml、storage/、logs/
/opt/taroko/clusters/<名稱>/router/  # tkctl.boot 與兩個開機腳本，依 cluster.yaml 產生；VyOS 自己寫的 config.boot 與 archive 也在這
/opt/taroko/cni/                 # CNI plugins，所有叢集共用
/opt/taroko/images/<K8s 版本>/   # 共用 image store，節點唯讀掛載；<版本>.conf 是它的 storage.conf
/etc/containers/systemd/<名稱>/  # Quadlet unit：<名稱>.network、<名稱>-router.container、每個節點一個 .container
/etc/systemd/system/<名稱>.target
/etc/systemd/system/<名稱>-routes.service   # 主機到 pod 與 service 網段的路由，跟 target 一起啟停
```

`TK_DATA_DIR` 換掉 `/opt/taroko`。第一次建立 `/opt/taroko` 需要 sudo，之後歸使用者擁有。

節點容器由 Quadlet 管理，每次啟停會重建；節點必須保留的 `/var`、`/etc`、`/usr/local/bin` 放在每節點的 named volume（`<節點>-var`、`-etc`、`-usr-local-bin`），`delete` 會一併移除。

`cluster.yaml` 的 `status` 區塊（`phase: Creating|Ready`、`completed:` 每個 phase 的完成時間、`tk8s:` 建立時的 tk8s 版本）由 tkctl 寫，`-f` 的檔不能有。v2026.10.x 建的叢集沒有 `status`，靠 `.create-complete` 標記視為 Ready，首次更新 `cluster.yaml`（新增或刪除節點）時補上並刪掉標記。

## router 與 apiserver 的入口

每個叢集有一個 VyOS 容器跟節點一起啟停：`<名稱>-router`，位址 `.253`。它是節點的預設閘道（連外流量經它轉給主機 bridge `.254`），也是 apiserver 的負載平衡：`load-balancing haproxy` 在 6443 接連線，對 `cluster.yaml` 裡的每一台 control plane 做 TCP 檢查、round-robin、TLS passthrough。`controlPlaneEndpoint` 與憑證都是 `api.<名稱>.k8s:6443`：每個節點把這個名字指到 `.253`（kubelet、Cilium 都走它），主機的 kubeconfig 直接寫 `.253`。這是 kubeadm 文件所畫的 HA 拓樸：一個 load balancer 在 control plane 前面，所有客戶端都連它。

tkctl 渲染的是 `tkctl.boot`；VyOS 開機前的 `vyos-preconfig-bootup.script` 把它複製成 `config.boot` 並補上 VyOS 自己產生的版本註腳，沒有這行 VyOS 會對檔案跑一輪 migration（加區塊、改寫檔案）。新增或刪除 control plane 時 tkctl 重新產生 `tkctl.boot`，以 `vbash` 做同一件事再 `load`、`commit`，VyOS 套用差異，既有連線不中斷；VyOS 的 `commit` 結束碼不可靠（沒有變更回 1、失敗反而回 0），所以 tkctl 等到 `show haproxy` 列出的 backend 正好是 cluster.yaml 的 control plane 才算完成，node add/delete 重跑時也以此判斷 router 是否已更新。`sudo podman exec -it <名稱>-router vbash` 進去就是 VyOS 的 CLI：`show configuration`、`show haproxy`、`show ip route`。

**為什麼是 TCP 檢查**：VyOS 的 haproxy 整合沒有 `check-ssl`，設 `ssl` 會把流量也包進 TLS、破壞 passthrough，`http-check` 又只能明文，所以打 `/readyz` 的健康檢查做不到。應用層的健康檢查本來就是負載平衡器而不是 router 的功能，雲端給 apiserver 的 NLB 也是 TCP 檢查。代價是 apiserver 起來但還沒 ready 的那幾秒流量仍會送過去，client 的重試會蓋掉；CP 停掉約 6 秒才標 DOWN。

跟 VIP 的差別：沒有 ARP、沒有 lease、沒有 static pod；單 control plane 的叢集也走同一條路，所以 `verify` 的 `[router]` 每個叢集都會檢查。pod 內不需要也無法解析 `api.<名稱>.k8s`（叢集內的 CoreDNS 擁有這個網域）：pod 走 `kubernetes` Service 的 ClusterIP。

## 主機的 sysctl

前置檢查會把主機的 `net.bridge.bridge-nf-call-iptables` 設為 0 並寫入 `/etc/sysctl.d/90-tk8s.conf`：節點之間的橋接流量不經主機 iptables，否則裝了 Docker 的主機（FORWARD 預設 DROP）會擋掉 pod 到其他節點的流量。`net.ipv4.ip_forward` 必須是 1：節點的網路命名空間建立時複製主機的這個值，未啟用時 kubeadm 的 preflight 會在節點裡失敗。

## sudo 用在哪

只用在必要處：`podman` 與 `systemctl`（節點是系統層級的 Quadlet unit）、寫 `/etc` 的 unit 與 sysctl 檔、寫 `/usr/local/bin`、第一次建立 `/opt/taroko`、刪叢集時移除節點以 root 寫下的資料。每一道都以 `$ sudo -n …` 印出。
