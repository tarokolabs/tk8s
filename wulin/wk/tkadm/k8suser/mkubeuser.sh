#!/bin/bash

[ "$#" != "1" ] && echo "mkubeuser.sh user" && exit 1
[ ! -d ${DIR_CSR} ] && mkdir -p ${DIR_CSR}

export STU=${1}
export DIR_CSR=/opt/zfs/k8suser/kuser
export K8SUSER=/opt/zfs/k8suser

which envsubst &>/dev/null 
[ $? = 1 ] && sudo apk update &>/dev/null && sudo apk add gettext &>/dev/null 

if [ -f ${DIR_CSR}/${STU}.key ]; then
   echo "K8S ${STU} existed" 
else 
   openssl genrsa -out ${DIR_CSR}/${STU}.key 2048 &>/dev/null
   openssl req -new -key ${DIR_CSR}/${STU}.key -out ${DIR_CSR}/${STU}.csr -subj "/CN=${STU}/O=${STU}"
   export BASE64_CSR=$(cat ${DIR_CSR}/${STU}.csr | base64 | tr -d '\n') 
   cat ${K8SUSER}/csr.yaml | envsubst | kubectl apply -f - &>/dev/null 
   kubectl certificate approve ${STU}-csr &>/dev/null
   sleep 5 
   kubectl get csr ${STU}-csr -o jsonpath='{.status.certificate}' | base64 -d > ${DIR_CSR}/${STU}.crt
   kubectl config set-credentials ${STU} --client-certificate=${DIR_CSR}/${STU}.crt --client-key=${DIR_CSR}/${STU}.key --kubeconfig=/home/${USER}/.kube/config &>/dev/null 
   kubectl config view | grep "name: ${STU}" &>/dev/null
   [ "$?" == "0" ] && echo "K8S ${STU} created" && echo "" && kubectl get csr ${STU}-csr
fi
