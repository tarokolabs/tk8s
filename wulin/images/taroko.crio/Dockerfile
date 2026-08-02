ARG VER=0.0
FROM docker.io/kindest/node:v${VER}

# FROM 之後要重新宣告才拿得到 build-arg
ARG VER
ARG CRIO_CHANNEL=stable
# 2026-07 起 pkgs.k8s.io 的 addons:/cri-o 路徑回 403（2026-08 複驗仍然如此），
# CRI-O deb 目前只剩上游 openSUSE Build Service 這個來源。做成 build-arg，
# 日後 pkgs.k8s.io 恢復時以 --build-arg CRIO_REPO=https://pkgs.k8s.io/addons:/cri-o 切回
ARG CRIO_REPO=https://download.opensuse.org/repositories/isv:/cri-o

# 與 taroko image 相同的除錯工具（gpg 是加 CRI-O repo 用的）
RUN apt-get update && apt-get upgrade -y
RUN apt-get install -y nano net-tools curl wget iputils-ping ipvsadm jq gpg
RUN rm /kind/manifests/*.yaml

# CRI-O 的 minor 版本必須對齊 K8s minor：VER=1.35.5 -> v1.35
# --force-confnew：cri-o 套件的 /etc/crictl.yaml 與 kind image 內建同名檔衝突，
# 不加會卡在 dpkg 的 conffile 互動提示（build 環境無 stdin 直接失敗）
RUN set -eux; \
    CRIO_URL="${CRIO_REPO}:/${CRIO_CHANNEL}:/v${VER%.*}/deb"; \
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL "${CRIO_URL}/Release.key" \
      | gpg --dearmor -o /etc/apt/keyrings/cri-o-apt-keyring.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/cri-o-apt-keyring.gpg] ${CRIO_URL}/ /" \
      > /etc/apt/sources.list.d/cri-o.list; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      -o Dpkg::Options::=--force-confnew cri-o; \
    rm -rf /var/lib/apt/lists/*

# 節點 container 內 inotify 上限
RUN printf 'fs.inotify.max_user_instances=8192\nfs.inotify.max_user_watches=524288\n' \
      > /etc/sysctl.d/99-taroko-inotify.conf

COPY crio.conf.d/ /etc/crio/crio.conf.d/
COPY crictl.yaml /etc/crictl.yaml
COPY storage.conf /etc/containers/storage.conf

# 共用唯讀 image store 的掛載點。目錄必須先存在：沒掛任何東西時它是空目錄，
# CRI-O 照常啟動（退化成沒有共用 store）；目錄不存在的話 containers/storage
# 會報 can't stat imageStore dir，crio 起不來。
RUN mkdir -p /var/lib/shared

# 換掉 CRI：關閉 containerd，改由 crio 提供 CRI socket
RUN systemctl disable containerd.service && \
    systemctl enable crio.service
