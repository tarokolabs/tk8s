#!/bin/bash

# [ `hostname` != "tkadm" ] && echo "pls login tkadm" && exit 1

[ "$#" != 1 ] && echo "mkcontext user" && exit 1

cat ~/.kube/config | grep "name: $1" &>/dev/null
[ "$?" != "0" ] && echo "$1 not exist" && exit 1

export K8SUSER=/opt/zfs/k8suser
export STU=$1
export MAS_IP=10.98.8.1
    
kubectl get namespace ${STU} &>/dev/null
[ $? != 0 ] && kubectl create namespace ${STU}

export CN=$(kubectl config view --minify -o jsonpath='{.clusters[].name}')

kubectl config set-context ${STU}-context --cluster=${CN} --namespace=${STU} --user=${STU} &>/dev/null
kubectl config view | grep -B 4 ${STU}-context
    
cat ${K8SUSER}/role.yaml | envsubst | kubectl apply -f -
cat ${K8SUSER}/rolebind.yaml | envsubst | kubectl apply -f -

cat ${K8SUSER}/clusterole.yaml | envsubst | kubectl apply -f -
cat ${K8SUSER}/clusterbind.yaml | envsubst | kubectl apply -f -
    
export K8SCRT=$(cat ${K8SUSER}/kuser/${STU}.crt| base64 |tr -d "\n";echo "")
export K8SKEY=$(cat ${K8SUSER}/kuser/${STU}.key| base64 |tr -d "\n";echo "")

cat ~/.kube/config | head -n 6 > ${K8SUSER}/kuser/${STU}.conf
cat ${K8SUSER}/context.temp | envsubst >> ${K8SUSER}/kuser/${STU}.conf
