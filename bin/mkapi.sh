#!/bin/bash

which yq &>/dev/null
[ "$?" != "0" ] && sudo apk update &>/dev/null && sudo apk add yq &>/dev/null

rm ~/.kube/*.yaml &>/dev/null

for api in 0 1 2 3 4
do
   n=$(yq eval ".clusters.[$api].name" ~/.kube/config)
   [ "$n" == null ] && break
   s=$(yq eval ".clusters.[$api].*.server" ~/.kube/config)
   c=$(yq eval ".clusters.[$api].*.certificate-authority-data" ~/.kube/config)

   echo "apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: $c
    server: $s
  name: $n" > ~/.kube/$n.yaml

done
