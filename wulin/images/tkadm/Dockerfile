ARG VER=00.00
FROM quay.io/cloudwalker/alp.base

# containers.conf  - The container engine (podman, buildah) configuration file specifies 
# default configuration options and command-line flags for container engines.
COPY "containers.conf" "/etc/containers/"
COPY web /opt
COPY goweb .gitconfig htpasswd config.yaml /var/tmp
RUN  mkdir -p /etc/skel/.ssh
COPY ssh/* /etc/skel/.ssh/

RUN apk update && apk upgrade && mkdir -p /lib/modules && mkdir -p /opt/www && \
    mv /opt/index.html /opt/www/ && mv /opt/cgi/ /opt/www/ && \
    apk add --no-cache openssh-server postgresql-client openrc tzdata shellcheck fuse-overlayfs podman \
    skopeo buildah k9s openssl mariadb-connector-c && \

    # 設定時區
    cp /usr/share/zoneinfo/Asia/Taipei /etc/localtime && \

    # setup openssh server
    ssh-keygen -t rsa -P "" -f /etc/ssh/ssh_host_rsa_key && \
    echo "Port 22100" >> /etc/ssh/sshd_config && \
    echo 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config && \
    echo 'PermitUserEnvironment yes' >> /etc/ssh/sshd_config && \
    echo 'StrictHostKeyChecking no' >> /etc/ssh/ssh_config && \ 

    # 建立管理者帳號 bigred
    adduser -h /home/bigred -s /bin/ash -G wheel -D bigred && \ 
    echo "%wheel ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers && \
    echo -e 'bigred\nbigred\n' | passwd bigred &>/dev/null && \
    mkdir -p /home/bigred/bin && cp /var/tmp/goweb /home/bigred/bin && \
    mkdir -p /home/bigred/.kube && chown bigred:root /home/bigred/.kube && \
    chown -R bigred:wheel /home/bigred/.ssh && chmod -R 700 /home/bigred/.ssh && \
    cp /var/tmp/.gitconfig /home/bigred/ && chown bigred:wheel /home/bigred/.gitconfig && \
    mkdir /home/bigred/wulin && chown bigred:wheel /home/bigred/wulin && \

    echo "export PATH=/home/bigred/.krew/bin:/home/bigred/wulin/bin:$PATH" >> /etc/profile && \
    echo "[ -f /opt/zfs/tkadm/system.sh ] && source /opt/zfs/tkadm/system.sh" >> /etc/profile && \
    echo 'bigred:100000:65535' > /etc/subuid && echo 'bigred:100000:65535' > /etc/subgid && \
    echo 'rbean:200000:65535' >> /etc/subuid && echo 'rbean:200000:65535' >> /etc/subgid && \
    echo 'gbean:300000:65535' >> /etc/subuid && echo 'gbean:300000:65535' >> /etc/subgid && \
    echo -e 'Welcome to Taroko K8S Console v25.12 (task,kubectl,mc,s3fs,regctl)\n' > /etc/motd && \
    rm /sbin/reboot && rm /usr/bin/killall
 
    # install task
RUN sh -c "$(curl -s --location https://taskfile.dev/install.sh)" -- -d -b "/home/bigred" && \
    chmod +x /home/bigred/task && mv /home/bigred/task /usr/local/bin/task

    # install kubectl & krew
RUN apk add kubectl --update-cache --repository http://dl-3.alpinelinux.org/alpine/edge/community/ --allow-untrusted && \
    apk add kubectl-krew --update-cache --repository http://dl-3.alpinelinux.org/alpine/edge/testing/ --allow-untrusted

RUN curl -L https://github.com/regclient/regclient/releases/latest/download/regctl-linux-amd64 >regctl && \
    chmod 755 regctl && mv regctl /usr/local/bin/

RUN echo "/usr/sbin/sshd" > /usr/bin/ash.service 

EXPOSE 22100

CMD ["/bin/ash"]
#ENTRYPOINT ["/usr/sbin/sshd"]
#CMD ["-D"] 
