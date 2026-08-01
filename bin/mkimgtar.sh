#!/bin/bash

mkdir -p ~/kindimg
# docker system prune -a -f &>/dev/null

bn="library/alpine:3.20.1 library/ubuntu:22.04 library/mysql:8.0.27 kindest/node:v1.29.8 kindest/node:v1.30.0 kindest/node:v1.24.17 kindest/node:v1.31.0"
kn=$(kubectl get pods -A -o jsonpath="{.items[*].spec['initcontainers','containers'][*].image}" | tr -s '[[:space:]]' '\n' | sort | uniq | grep -v quay.io | grep -v registry.k8s)
for img in $bn $kn
do
  imgfn=$(echo $img | tr '/' '-')
  if [ ! -f ~/kindimg/${imgfn}.tar ]; then
     sudo podman pull $img &>/dev/null
     [ "$?" == "0" ] && sudo podman save $img > ~/kindimg/${imgfn}.tar && echo "~/kindimg/${imgfn}.tar"
  fi
done
