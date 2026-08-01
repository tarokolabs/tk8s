#!/bin/bash

kubectl config unset users.${1}
kubectl delete csr ${1}-csr

