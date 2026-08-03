#!/bin/bash

sudo podman login quay.io
# build taroko images
for n in 1.33.12 1.34.8 1.35.5 1.36.1 
do
  sudo podman rmi quay.io/cloudwalker/taroko:v${n} &>/dev/null
  sudo podman build --build-arg VER=${n} --no-cache --force-rm -t quay.io/cloudwalker/taroko:v${n} \
  ${TK_HOME}/wulin/images/taroko/ &>/dev/null
  if [ "$?" == "0" ]; then
     sudo podman push quay.io/cloudwalker/taroko:v${n} &>/dev/null
     [ "$?" == "0" ] && echo "quay.io/cloudwalker/taroko:v${n} ok"
  fi
done

sudo podman logout quay.io
