# 節點 image、router image 與共用 image store

## 節點 image

每個節點容器使用的 image 是 `ghcr.io/tarokolabs/tk8s/node:v<K8s 版本>`，配方在 `images/node/`，以 `docker.io/library/debian:13-slim` 為基底：

- **systemd** 當 PID 1（Podman 看到 `/sbin/init` 自動啟用 systemd 模式），加上 dbus、iptables、nftables、conntrack 這些 kubeadm 與 kubelet 需要的作業系統工具，以及 nano、vim、ping、less、jq、tcpdump、strace。
- **CRI-O** 來自官方的 static bundle（`storage.googleapis.com/cri-o/artifacts/cri-o.amd64.<版本>.tar.gz`），版本由 `versions.yaml` 的 `crio` 表依 K8s minor 決定，下載後比對 `.sha256sum`。crun、conmon、runc 都在 bundle 裡。
- **kubeadm、kubelet、kubectl** 來自 `dl.k8s.io/release/v<版本>/bin/linux/amd64/`，各自比對 `.sha256`。kubelet 的 unit 與 kubeadm 的 drop-in 是 kubeadm 文件的標準內容，`--runtime-cgroups` 指向 `crio.service`。kubelet 另外讀 `--config-dir=/etc/kubernetes/kubelet.conf.d`，tkctl 把節點的 `kubeReserved` 掛在那裡（見架構文件的「節點看到的規格」）。
- **FRR**（Debian 的 `frr` 套件，10.x）與 `tk8s-apiserver-health.timer`：bgp 模式的 control plane 才啟用（`tkctl` 在 `kubeadm/frr` 寫入 `/etc/frr/frr.conf`、`/etc/tk8s/vip` 並 enable），image 裡兩者都是 disabled，worker 與 proxy 叢集不會碰到。timer 每秒打本機 `/readyz`，連續 3 次 ok 才把 VIP 放到 `lo`、連續 2 次失敗才拿掉；FRR 宣告的就是 lo 上的位址。
- **不包含**：containerd、預載的容器 image（叢集的 image 來自共用 image store）、kind 的任何元件。image 約 671 MB。

每個元件都是固定版本的 artifact 加 checksum，所以同一份 `versions.yaml` 一年後重建仍是同樣的 CRI-O 與 Kubernetes（apt 安裝的套件隨 Debian 的 point release 更新，沒有固定）。

`tk8s-node-init.service` 在節點啟動時做 kind 的 entrypoint 以前做的兩件事：把根掛載設成 rshared（kubelet 的掛載才能傳播出去）、給節點一個專屬的 `product_uuid`（同主機的容器看到的是主機的 DMI；kubeadm 要求每個節點唯一）。`/etc/machine-id` 在 image 裡是空的，第一次啟動由 systemd 產生並留在節點的 `-etc` volume。

節點內的短名稱 image（`kubectl run --image=nginx`）一律解析到 `docker.io`。

### Podman 的 systemd 模式

容器的進入點是 `/sbin/init` 時，podman 自動進 systemd 模式（`--systemd` 預設是自動判斷，`always` 可強制），替 systemd 準備它開機時假設存在、但一般容器沒有的東西：

- `/run`、`/run/lock`、`/tmp`、`/var/log/journal` 掛成 tmpfs，systemd 在這些地方寫 socket、PID 檔與 journal。
- 容器自己的 cgroup 子樹以可寫方式掛進 `/sys/fs/cgroup`（cgroup v2 下就是把子樹委派給容器），節點內的 systemd 才能替每個 unit 建 cgroup。CRI-O 用 systemd cgroup driver，透過 sd-bus 請 systemd 建 cgroup，所以 image 要裝 dbus。
- 停止訊號是 `SIGRTMIN+3`（image 的 `STOPSIGNAL`），systemd 收到就走正常關機：先停 kubelet 與 CRI-O，再退出；`node.container` 的 `StopTimeout=60` 是給這段流程的時間，沒收到訊號的 SIGTERM 會直接殺掉節點。
- 環境變數 `container=podman`，systemd 看到就知道自己在容器裡，略過硬體相關的 unit；udev、getty、apt 的 timer 另外在 image 裡 mask 掉。

kind 的節點同樣是 systemd 當 PID 1，但用自己的 entrypoint 腳本做這些準備；這裡交給 podman，image 只剩 `tk8s-node-init.service` 做 podman 不做的兩件事：根掛載設成 rshared、給節點一個自己的 `product_uuid`。

### 自己 build

```bash
sudo podman build --build-arg VER=1.37.1 --build-arg CRIO=v1.37.2 \
  -t ghcr.io/tarokolabs/tk8s/node:v1.37.1 images/node
```

`tkctl pull images --k8s <版本>` 在 tag 沒發布時會自動做同一件事。`--k8s` 指定的 minor 必須在 `versions.yaml` 的 `crio` 表裡才能建置；已下載到本機的 image 不受此限。

CI 的 `release` workflow 在每個 tag 以同一份配方建兩個支援版本的 image 推上 GHCR，release notes 列出 digest。

## 共用 image store

建叢集需要的 image（control plane、Cilium、metrics-server、local-path、`verify` 的測試 image）由主機拉一次，放在 `/opt/taroko/images/<K8s 版本>/`，每個節點容器以唯讀掛在 `/var/lib/containers/shared`，CRI-O 把它當 additional image store：查 image 先看這裡，有就直接用，沒有才照常 pull 進節點自己的 `/var`。

- 第一次 `tkctl create cluster` 多花一次下載（約 2 GB、一兩分鐘），之後每個叢集、每個節點都是零下載；`--k8s` 不同版本各有一個目錄。
- 離線的教室先在有網路時 `tkctl pull images` 與 `tkctl create cluster --phase nodes/cni-plugins`（CNI plugins 不在 store 裡），上課時 `TK_OFFLINE=1 tkctl create cluster`：缺任何 image 會直接列出而不嘗試下載。CI 的 e2e 就是這樣建的。`--gvisor` 的叢集另外需要 gVisor 的下載，目前不受 `TK_OFFLINE` 管。
- 唯讀是刻意的：多個 CRI-O 寫同一個 store 沒有安全的用法（節點沒乾淨關機時 `crio wipe` 會清空整個 storage 目錄）。學員 `kubectl run` 自行拉取的 image 進節點本身的 volume，不會污染共用目錄。
- 清單不是手寫的：`tkctl get images --k8s <版本>` 從 `versions.yaml`、node image 裡的 `kubeadm config images list`、`cilium install --dry-run` 與 `manifests/` 動態計算（算這份清單要用到 node image 與 cilium CLI，所以 `get images` 只看不拉：兩者不在主機上時會請你先跑 `tkctl pull images`；輸出的前兩列是 node image 與 router image 在主機上的狀態，其餘是 store 的清單）。
- 建叢集時主機要套用的 manifest 也都在 repo 裡：Gateway API CRD、metrics-server manifest 與 Cilium 的 Helm chart 由 `scripts/vendor-manifests.sh` 放進 `manifests/`，升版時重新執行一次；cilium CLI 不會再向 helm.cilium.io 下載 chart。
- 只有 tkctl 寫這個目錄；主機的 `podman image prune`、`podman system reset` 操作的是 `/var/lib/containers/storage`，不會影響它。要刪除就 `tkctl delete images --k8s <版本>`，有叢集在用會被拒絕。
- 節點 image 需是 2026-10 之後的配方（掛載點 `/var/lib/containers/shared`）。先前下載的本機快取用 `tkctl pull images --refresh` 更新（同一個 tag 重發時也是這樣換掉舊的）；舊 image 仍可建叢集，只是不會用到 store。

## router image

每個叢集的 router 跑 `ghcr.io/tarokolabs/tk8s/router:<建置編號>`，配方在 `images/router/`。VyOS 沒有可直接跑的容器 image，只有 nightly 的 ISO，所以配方把 ISO 裡的 `live/filesystem.squashfs` 取出來當 rootfs，mask 掉容器裡起不來的 auditd 與 rsyslog，`STOPSIGNAL SIGRTMIN+3`、`CMD /sbin/init`，約 1.8 GB。`versions.yaml` 的 `vyos` 是 nightly tag 去掉 `-rolling` 的建置編號，`vyos_sha256` 是 ISO 的 checksum；每次發版推到當時最新的 nightly，release 跟節點 image 一起建、推 GHCR、digest 進 release notes。tag 沒發布時 `tkctl pull images` 會從 ISO 本地建（下載 641 MB，幾分鐘）。它跑在主機的 podman storage，不進共用 store。
