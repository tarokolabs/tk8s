FROM docker.io/kindest/node:v1.30.0
RUN apt update && apt -y upgrade && apt install -y gpg inotify-tools nano && \
    echo "fs.inotify.max_user_instances=8192" >> /etc/sysctl.conf && \
    echo "fs.inotify.max_user_watches=524288" >> /etc/sysctl.conf && \
    echo "deb [signed-by=/etc/apt/keyrings/cri-o-apt-keyring.gpg] https://pkgs.k8s.io/addons:/cri-o:/stable:/v1.30/deb/ /" > /etc/apt/sources.list.d/cri-o.list && \
    curl -fsSL https://pkgs.k8s.io/addons:/cri-o:/stable:/1.30/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/cri-o-apt-keyring.gpg && \
    apt update && apt install -y cri-o && systemctl enable crio.service && \

