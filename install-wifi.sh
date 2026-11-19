#!/bin/sh
set -eu

cd -- "$(dirname -- "$0")"
sudo -v

sudo install -D -m 0755 wifi-switch.py /usr/local/libexec/capri-wifi-switch.py
sudo install -m 0644 capri-wifi.socket capri-wifi@.service /etc/systemd/system/
sudo install -d -m 0755 /etc/systemd/system/capri-wifi.socket.d
printf '[Socket]\nSocketUser=%s\n' "$(id -un)" |
    sudo tee /etc/systemd/system/capri-wifi.socket.d/owner.conf >/dev/null
sudo chmod 0644 /etc/systemd/system/capri-wifi.socket.d/owner.conf
sudo systemctl daemon-reload
sudo systemctl enable --now capri-wifi.socket

dashboard="$HOME/.local/share/capri-dashboard"
install -d "$dashboard"
install -m 0644 server.py index.html "$dashboard/"
systemctl --user restart capri-dashboard.service
systemctl --user is-active capri-dashboard.service
printf '\nMở dashboard trên cổng 8765 và chạy lệnh sau để lấy mã quản trị.\n'
printf 'python3 ~/.local/share/capri-dashboard/server.py --admin-code\n'
