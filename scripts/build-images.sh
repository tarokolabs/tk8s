#!/bin/bash
# 建置並推送 tk8s 平台 image 至 GHCR（ghcr.io/tarokolabs/tk8s/*）
#
# 用法：
#   build-images.sh node <K8s版本>...     # 每個版本建 node/crio 與 node/containerd
# （toolbox/admin 等叢集內 image 屬教材範疇，見 wulin repo 的 bin/build-images.sh）
#
# 環境變數：
#   TK        repo 根目錄（預設依腳本位置自我定位）
#   SUDO      前綴指令（預設 sudo；rootless podman 環境設為空字串）
#   DIGEST_DIR 若設定，將各 image 的 digest 寫入該目錄（供 CI 彙整 release notes）
#
# 推送前需先登入：podman login ghcr.io
# 命名規則見 https://github.com/tarokolabs/tk8s/issues/20

REG="ghcr.io/tarokolabs/tk8s"
LABELS="--label=org.opencontainers.image.source=https://github.com/tarokolabs/tk8s \
        --label=org.opencontainers.image.licenses=GPL-2.0-or-later"
TK="${TK:-$(cd "$(dirname "$(realpath "$0")")/.." && pwd)}"
SUDO="${SUDO-sudo}"

build_push() {  # <name:tag> <context 目錄> [額外 build 參數...]
   local ref="${REG}/${1}" dir="${TK}/${2}" safe="$(echo ${1} | tr '/:' '--')"
   local log="/tmp/build-${safe}.out"
   shift 2
   echo "building ${ref}"
   ${SUDO} podman build --format=docker --no-cache --force-rm ${LABELS} "$@" \
        -t "${ref}" "${dir}" &>"${log}"
   [ "$?" != "0" ] && echo "  build failed（見 ${log}）" && tail -5 "${log}" && exit 1
   ${SUDO} podman push --digestfile "${log}.digest" "${ref}" &>/dev/null
   [ "$?" != "0" ] && echo "  push failed（先 podman login ghcr.io）" && exit 1
   echo "  pushed ${ref}  $(cat ${log}.digest)"
   if [ -n "${DIGEST_DIR}" ]; then
      mkdir -p "${DIGEST_DIR}"
      echo "${ref}  $(cat ${log}.digest)" > "${DIGEST_DIR}/${safe}.digest"
   fi
}

case "$1" in
node)
   shift
   [ "$1" == "" ] && echo "build-images.sh node <K8s版本>..." && exit 1
   for v in "$@"; do
      build_push "node/containerd:v${v}" images/node-containerd --build-arg=VER=${v}
      build_push "node/crio:v${v}"       images/node-crio       --build-arg=VER=${v}
   done
   ;;
*)
   echo "build-images.sh node <K8s版本>..."
   exit 1
   ;;
esac
