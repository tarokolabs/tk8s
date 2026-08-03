#!/bin/bash
# 建置並推送 tk8s 平台 image 至 GHCR（ghcr.io/tarokolabs/tk8s/*）
#
# 用法：
#   build-images.sh tools [CalVer]        # toolbox、admin（預設 v2026.8.0）
#   build-images.sh node <K8s版本>...     # 每個版本建 node/crio 與 node/containerd
#
# 推送前需先登入：podman login ghcr.io
# 命名規則見 https://github.com/tarokolabs/tk8s/issues/20

REG="ghcr.io/tarokolabs/tk8s"
LABELS="--label=org.opencontainers.image.source=https://github.com/tarokolabs/tk8s \
        --label=org.opencontainers.image.licenses=GPL-2.0"
TK=~/tk
ALPINE_VER="3.22.1"

build_push() {  # <name:tag> <context 目錄> [額外 build 參數...]
   local ref="${REG}/${1}" dir="${TK}/${2}" log="/tmp/build-$(echo ${1} | tr '/:' '--').out"
   shift 2
   echo "building ${ref}"
   sudo podman build --format=docker --no-cache --force-rm ${LABELS} "$@" \
        -t "${ref}" "${dir}" &>"${log}"
   [ "$?" != "0" ] && echo "  build failed（見 ${log}）" && exit 1
   sudo podman push --digestfile "${log}.digest" "${ref}" &>/dev/null
   [ "$?" != "0" ] && echo "  push failed（先 podman login ghcr.io）" && exit 1
   echo "  pushed ${ref}  $(cat ${log}.digest)"
}

case "$1" in
tools)
   CALVER="${2:-v2026.8.0}"
   build_push "toolbox:${CALVER}" wulin/images/base --build-arg=VER=${ALPINE_VER}
   build_push "admin:${CALVER}"   wulin/images/tkadm
   ;;
node)
   shift
   [ "$1" == "" ] && echo "build-images.sh node <K8s版本>..." && exit 1
   for v in "$@"; do
      build_push "node/containerd:v${v}" wulin/images/taroko      --build-arg=VER=${v}
      build_push "node/crio:v${v}"       wulin/images/taroko.crio --build-arg=VER=${v}
   done
   ;;
*)
   echo "build-images.sh tools [CalVer] | node <K8s版本>..."
   exit 1
   ;;
esac
