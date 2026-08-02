ARG IMG_VERSION

FROM quay.io/flysangel/image:ubuntu.sshd-${IMG_VERSION}

USER root

RUN \
    # install lxqt
    apt update --fix-missing && \
    DEBIAN_FRONTEND=noninteractive apt upgrade -y && \
    DEBIAN_FRONTEND=noninteractive apt install -y --no-install-recommends --allow-unauthenticated lxqt-core && \
    DEBIAN_FRONTEND=noninteractive apt install -y lxqt-theme-debian openbox && \
    DEBIAN_FRONTEND=noninteractive apt install -y \
    bash-completion \
    x11vnc xvfb novnc \
    fcitx fcitx-chewing dbus dbus-x11 \
    x11-utils x11-xserver-utils \
    ttf-wqy-zenhei language-pack-zh* language-pack-gnome-zh*

RUN \
    # clean
    apt purge -y htop* info* byobu* lightdm* lxrandr* lxappearance* xscreensaver* xfconf* language-selector* ibus* xfwm4* && \
    apt autoremove -y && \
    apt clean -y && \
    apt autoclean -y && \
    rm -r /var/lib/apt/lists/*

RUN \
    # set openbox
    cp /etc/xdg/openbox/rc.xml /etc/xdg/openbox/rc.xml.bk && \
    sed -i 's|sans|Ubuntu Mono|g' /etc/xdg/openbox/rc.xml && \
    sed -i 's|<size>8</size>|<size>12</size>|g' /etc/xdg/openbox/rc.xml && \
    sed -i 's|<size>9</size>|<size>14</size>|g' /etc/xdg/openbox/rc.xml && \
    sed -i 's|<number>4</number>|<number>2</number>|g' /etc/xdg/openbox/rc.xml

RUN \
    # set menu
    cp /etc/xdg/menus/lxqt-applications.menu /etc/xdg/menus/lxqt-applications.menu.bk && \
    sed -i 's|<!-- Other -->|<!-- Other|g' /etc/xdg/menus/lxqt-applications.menu && \
    sed -i 's|<!-- End Other -->|End Other -->|g' /etc/xdg/menus/lxqt-applications.menu && \
    sed -i 's|<!-- Leave -->|<!-- Leave|g' /etc/xdg/menus/lxqt-applications.menu && \
    sed -i 's|<!-- End Leave -->|End Leave -->|g' /etc/xdg/menus/lxqt-applications.menu && \
    sed -i 's|<!-- Screensaver -->|<!-- Screensaver|g' /etc/xdg/menus/lxqt-applications.menu && \
    sed -i 's|<!-- End Screensaver -->|End Screensaver -->|g' /etc/xdg/menus/lxqt-applications.menu

RUN \
    # set mks
    wget -qO - https://gitlab.com/flysangel/srv_config/raw/main/ubuntu/app.mks/app.mks.txt | bash

RUN \
    # download novnc
    wget https://gitlab.com/flysangel/srv_config/raw/main/ubuntu/install.lxqt/novnc/index.html -qO /usr/share/novnc/index.html

USER bigred
WORKDIR /home/bigred

ENV \
    WINDOWS_W=1920 \
    WINDOWS_H=1080 \
    VNC_PASSWD=vnc520 \
    # terminal default shell
    SHELL=/bin/bash

RUN \
    # set profile
    mkdir -p ~/.config/autostart && \
    mkdir -p ~/.config/fcitx && \
    mkdir -p ~/.config/lxqt && \
    wget https://gitlab.com/flysangel/srv_config/raw/main/ubuntu/install.lxqt/config/autostart/fcitx.desktop -qO ~/.config/autostart/fcitx.desktop && \
    wget https://gitlab.com/flysangel/srv_config/raw/main/ubuntu/install.lxqt/config/fcitx/profile -qO ~/.config/fcitx/profile && \
    wget https://gitlab.com/flysangel/srv_config/raw/main/ubuntu/install.lxqt/config/lxqt/lxqt.conf -qO ~/.config/lxqt/lxqt.conf && \
    wget https://gitlab.com/flysangel/srv_config/raw/main/ubuntu/install.lxqt/config/lxqt/panel.conf -qO ~/.config/lxqt/panel.conf && \
    wget https://gitlab.com/flysangel/srv_config/raw/main/ubuntu/install.lxqt/config/lxqt/session.conf -qO ~/.config/lxqt/session.conf && \
    x11vnc -storepasswd ${VNC_PASSWD} ~/x11vnc.pass

CMD [ \
    "/bin/bash", \
    "-c", \
    " \
    # sshd
    sudo /usr/sbin/sshd -D"]
