ARG VER=0.0
FROM quay.io/cloudwalker/alp.base
RUN apk update && apk upgrade && \
    apk add --no-cache openssh-server tzdata && \
    # 設定時區
    cp /usr/share/zoneinfo/Asia/Taipei /etc/localtime && \
    ssh-keygen -t rsa -P "" -f /etc/ssh/ssh_host_rsa_key && \

    echo "Port 22101" >> /etc/ssh/sshd_config && \
    echo 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config && \
    echo 'PermitUserEnvironment yes' >> /etc/ssh/sshd_config && \
    echo 'StrictHostKeyChecking no' >> /etc/ssh/ssh_config && \

    echo "/usr/sbin/sshd" > /usr/bin/ash.service && \

    echo -e 'Welcome to ssh console\n' > /etc/motd && \
    # 建立管理者帳號 bigred
    adduser -s /bin/ash -h /home/bigred -G wheel -D bigred && \ 
    echo "%wheel ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers && \
    echo -e 'bigred\nbigred\n' | passwd bigred &>/dev/null && \
    mkdir -p /home/bigred/wulin && chown bigred:wheel /home/bigred/wulin && \
    echo "export ENV=/usr/bin/ash.rc" >> /etc/profile && \ 
    rm /sbin/reboot && rm /usr/bin/killall

# ENV ENV=/usr/bin/ash.rc

EXPOSE 22101

CMD ["/bin/ash"]

#ENTRYPOINT ["/usr/sbin/sshd"]
#CMD ["-D"] 
