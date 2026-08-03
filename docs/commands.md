# tkctl 命令手冊

Taroko 平台的統一命令入口是 `tkctl <名詞> <動詞> [參數…]`。所有子命令都是可讀的 shell 腳本（`libexec/tkctl/<名詞>/<動詞>`）——想知道平台實際做了什麼，直接打開對應檔案就看得到 kubeadm 被餵了什麼。

`tkctl help` 隨時列出完整子命令樹（自 `libexec/` 自動生成，不會與實際漂移）。

**既有短命令全數保留**：`kto`、`kls`、`ksc`⋯⋯是等價的相容 shim，教材與肌肉記憶不受影響。完整對照見文末。

## 快速開始

```bash
tkctl cluster create tk8s 1.36.1   # 建三節點叢集（約 8 分鐘）
tkctl cluster list                 # 看狀態
tkctl cluster stop tk8s            # 下班
tkctl cluster start tk8s           # 上班
tkctl cluster delete tk8s          # 拆掉
```

## cluster——叢集生命週期

| 命令 | 說明 |
|---|---|
| `tkctl cluster create <叢集名> [K8s版本]` | 建整套叢集：檢查 swap → 下載工具（CNI plugin、kubectl、cilium、helm）→ 建節點容器 → `kubeadm init`/`join` → CNI（預設 cilium，kube-proxy replacement）＋ LB-IPAM ＋ Envoy Gateway ＋ RuntimeClass ＋ metrics-server ＋ local-path → 教材 repo（`WULIN_DIR`）在場時自動部署管理主機（tkadm）與私有 registry（dkreg）。版本不指定時用 conf 的預設值 |
| `tkctl cluster delete <叢集名>` | 刪除叢集（節點容器＋網路） |
| `tkctl cluster stop <叢集名>` / `start <叢集名>` | 停止／啟動叢集容器，狀態保留 |
| `tkctl cluster list` | 列出叢集狀態、pods 與既有備份 |
| `tkctl cluster switch <叢集名>` | 把 `~/.kube/config` 切到指定叢集（多叢集並存時用） |
| `tkctl cluster backup <叢集名>` | **叢集級**備份：每個節點 `podman commit` 成 image tar ＋ volume tar，存於 `clusters/<叢集名>-bak-<版本>/` |
| `tkctl cluster restore <叢集名> <版本>` | 從上述備份完整還原 |
| `tkctl cluster kubeconfig <叢集名>` | 從 control-plane 重抓 admin.conf 到 `~/.kube/config` |
| `tkctl cluster create-nodes` | 內部子命令（被 create 引用）：建 podman 網路與節點容器、節點 image 三段備援（本地 → GHCR 拉取 → 本地配方建置） |

## node——節點

| 命令 | 說明 |
|---|---|
| `tkctl node add <叢集名>` | 加入節點——External 網路型態（跨主機）專用，經 sshpass 連線遠端主機 |

## net——網路

| 命令 | 說明 |
|---|---|
| `tkctl net setup <叢集名>` | 叢集網路收尾：節點 `/etc/hosts`、DNAT 腳本執行（create 已自動呼叫，異動後可單獨重跑） |
| `tkctl net gateway-create <叢集名>` | 產生 `tkadd-dnat.sh`／`tkdel-dnat.sh`（外部流量 DNAT 進叢集） |
| `tkctl net gateway-delete <叢集名>` | 移除 worker2 上的 `tkgw` macvlan 介面 |
| `tkctl net route-add` / `route-delete` | 主機側 pod 網段（10.244.x）路由的加與刪 |
| `tkctl net expose` | 主機 ipvsadm 轉發（管理主機與監控服務的 NodePort 曝露；需 `IP` 環境變數與 ipvsadm） |

## lab——教學運維

| 命令 | 說明 |
|---|---|
| `tkctl lab backup <lab名>` | 備份**學員家目錄**與帳號檔（在管理主機 pod 內執行）——注意與 `cluster backup`（整座叢集快照）是不同層級 |
| `tkctl lab restore <lab名>` | 還原學員家目錄與帳號 |
| `tkctl lab mount (create\|remove\|list)` | 管理 s3fs 掛載（經管理主機） |

## tool——雜項

| 命令 | 說明 |
|---|---|
| `tkctl tool psql <SQL>` | 對叢集內 PostgreSQL 下查詢 |
| `tkctl tool svc-ip [service名]` | 查 service 的 NodePort 對外位址 |

## 新舊命令對照表

| 舊命令 | 新命令 | | 舊命令 | 新命令 |
|---|---|---|---|---|
| `kto` | `tkctl cluster create` | | `kns` | `tkctl net setup` |
| `kgn` | `tkctl cluster delete` | | `kgw` | `tkctl net gateway-create` |
| `kco` | `tkctl cluster stop` | | `kdg` | `tkctl net gateway-delete` |
| `kci` | `tkctl cluster start` | | `kra` | `tkctl net route-add` |
| `kls` | `tkctl cluster list` | | `krd` | `tkctl net route-delete` |
| `ksc` | `tkctl cluster switch` | | `kiv` | `tkctl net expose` |
| `kcg` | `tkctl cluster backup` | | `kbk` | `tkctl lab backup` |
| `krs` | `tkctl cluster restore` | | `krt` | `tkctl lab restore` |
| `kpk` | `tkctl cluster kubeconfig` | | `kmt` | `tkctl lab mount` |
| `kcn` | `tkctl cluster create-nodes` | | `pql` | `tkctl tool psql` |
| `kan` | `tkctl node add` | | `getnpip` | `tkctl tool svc-ip` |

## 設定檔（conf/*.conf）

每個環境一個檔案，以 shell 變數宣告叢集拓樸。載入時自動驗證（`tk_load_conf`）：檔案不存在會列出可用環境，必要欄位缺漏、`NID` 非 CIDR、`NGW` 非 IPv4 都會具名報錯。

| 變數 | 說明 |
|---|---|
| `KVER` | 預設 K8s 版本（命令列參數可覆寫） |
| `K8SCRI` | Container runtime：`crio`（預設）或 `containerd` |
| `IMG` | 節點 image（依 `K8SCRI` 自動對應 GHCR 名稱） |
| `K8SCNI` | CNI：`cilium`（預設）或 `canal` |
| `NID` / `NGW` / `SNID` | 節點網段（CIDR）／閘道／service 網段 |
| `LCTN` | 節點清單：`IP:名稱:記憶體:CPU`，空白分隔（External 型態用 `ECTN`） |
| `NTP` | 網路型態：`Internal`（單機 bridge）或 `External`（跨主機 macvlan） |
| `TKIND` | 叢集種類（`K8S`） |

新增環境：複製一份 `.conf` 改網段與節點清單即可。

## 路徑與環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `TK_HOME` | 依 CLI 位置自我定位 | repo 安裝位置——clone 到哪都能跑，僅在需要顯式指定時設定 |
| `TAROKO_HOME` | `/opt/taroko` | 主機側狀態根：`clusters/<叢集名>/`（狀態與 PVC 資料）、`cni/`（工具下載）、`wulin/` |
| `WULIN_DIR` | `${TAROKO_HOME}/wulin` | 教材 repo 位置；在場時 create 會自動部署管理主機 |

節點容器內看到的是同一套語彙：主機的 `clusters/<叢集名>` 掛載為節點內的 `/opt/taroko`，教材掛載為 `/opt/taroko/wulin`。
