# Korovki remote access runbook

Этот документ переносимый: не коммить сюда реальные IP, пароли, приватные ключи и экспортированные `.conf` из Amnezia.

## 1. Переменные установки

Перед работой подставь свои значения:

```text
SERVER_IP=<SERVER_IP>
SSH_PORT=22
SSH_USER=root
AMNEZIAWG_PORT=1234
RPI_INTERFACE=awg0
RPI_VPN_CIDR=<VPN_CIDR>
RPI_USER=<RPI_USER>
```

`RPI_VPN_CIDR` — VPN-сеть, которую Raspberry Pi должна видеть через AmneziaWG. Для service-only доступа это должна быть внутренняя VPN-сеть Amnezia, а не `0.0.0.0/0`.

## 2. Серверный порядок

1. Провести аудит по `SERVER_AUDIT_TEMPLATE.md`.
2. Создать backup важных конфигов.
3. Проверить, что сайт отвечает после backup.
4. Установить self-hosted Amnezia через AmneziaVPN на ноутбуке.
5. В настройках AmneziaWG выставить `AMNEZIAWG_PORT`, если приложение выбрало другой порт.
6. Проверить, что сайт и backend после установки живы.

## 3. Установка AmneziaWG на сервер через ноутбук

В AmneziaVPN:

1. Нажми `+`.
2. Выбери `Self-hosted VPN`.
3. Введи:

```text
Server IP: <SERVER_IP>
SSH port: <SSH_PORT>
Username: <SSH_USER>
Password: <SERVER_PASSWORD>
```

4. Выбери автоматическую установку, если она доступна.
5. После установки открой настройки серверного подключения.
6. Открой настройки протокола `AmneziaWG`.
7. Поставь порт:

```text
1234
```

8. Сохрани настройки и подключись к VPN с ноутбука.

## 4. Доступ для ноутбука администратора

Создай отдельный guest access для ноутбука:

```text
Name: admin-laptop
Protocol: AmneziaWG
Format: For AmneziaVPN app
```

Импортируй полученный доступ в AmneziaVPN и подключись.

Проверка после настройки Raspberry Pi:

```powershell
ping RPI_VPN_IP
ssh RPI_USER@RPI_VPN_IP
```

## 5. Native config для Raspberry Pi

Создай отдельный guest access:

```text
Name: raspberry-pi
Protocol: AmneziaWG
Format: AmneziaWG native format
```

На выходе нужен файл вида:

```text
amnezia_for_awg.conf
```

Этот файл нельзя коммитить в git. В `.gitignore` уже добавлены правила для `.conf`, ключей и локальных секретов в `remote_access/amnezia`.

## 6. Full tunnel в экспортированном конфиге

Amnezia часто экспортирует:

```text
AllowedIPs = 0.0.0.0/0, ::/0
```

Для Raspberry Pi это опасно: весь интернет малины может пойти через VPN, включая телеметрию и связь через SIM.

Для service-only доступа ставь конфиг так:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_amnezia_client.sh \
  --config ./remote_access/amnezia/amnezia_for_awg.conf \
  --interface awg0 \
  --service-allowed-ips <VPN_CIDR>
```

Пример, если внутренняя VPN-сеть Amnezia — `10.8.1.0/24`:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_amnezia_client.sh \
  --config ./remote_access/amnezia/amnezia_for_awg.conf \
  --interface awg0 \
  --service-allowed-ips 10.8.1.0/24
```

Скрипт не меняет исходный `.conf`; он ставит в `/etc/amnezia/amneziawg/awg0.conf` уже подготовленную копию.

Если exported config содержит пустые поля вида `I2 =`, `I3 =`, `I4 =`, `I5 =`, установщик пропустит их в установленной копии. Это нужно, потому что `awg setconf` не принимает пустые AmneziaWG-поля.

## 7. Установка на Raspberry Pi

Если AmneziaWG уже установлен:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_amnezia_client.sh \
  --config ./remote_access/amnezia/amnezia_for_awg.conf \
  --interface awg0 \
  --service-allowed-ips <VPN_CIDR>
```

Если `awg` / `awg-quick` ещё не установлены:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_amnezia_client.sh \
  --config ./remote_access/amnezia/amnezia_for_awg.conf \
  --interface awg0 \
  --service-allowed-ips <VPN_CIDR> \
  --install-packages
```

Не используй `--allow-default-route` без отдельной проверки.

## 8. Проверка на Raspberry Pi

```bash
sudo systemctl status amneziawg-client@awg0 --no-pager
sudo journalctl -u amneziawg-client@awg0 -n 100 --no-pager
sudo awg show awg0
ip addr show awg0
ip route show table all | grep awg0
```

Полная диагностика:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/check_remote_access.sh --interface awg0
```

## 9. SSH и VNC через VPN

Когда ноутбук и Raspberry Pi подключены к AmneziaWG:

```powershell
ping RPI_VPN_IP
ssh RPI_USER@RPI_VPN_IP
```

Если VNC включён:

```text
RPI_VPN_IP:5900
```

## 10. Reverse SSH fallback

Этот путь нужен, если оба клиента подключены к AmneziaWG, но прямой доступ между ними не работает.

Идея:

```text
Raspberry Pi -> Server
server 127.0.0.1:2222 -> Raspberry Pi 127.0.0.1:22
server 127.0.0.1:5901 -> Raspberry Pi 127.0.0.1:5900
```

Порты `2222` и `5901` должны слушать только `127.0.0.1` на сервере. Не включай `GatewayPorts yes` без отдельного решения.

На сервере нужен отдельный пользователь, например:

```text
pi-tunnel
```

На сервере подготовь или обнови каталог с инструментами:

```bash
if [ -d /opt/host-monitor-remote-tools/.git ]; then
  cd /opt/host-monitor-remote-tools
  git pull
else
  git clone https://github.com/idkotin/malina_for_korovki.git /opt/host-monitor-remote-tools
  cd /opt/host-monitor-remote-tools
fi
```

Потом подготовь пользователя `pi-tunnel`:

```bash
sudo bash ./remote_access/amnezia/prepare_reverse_ssh_server.sh \
  --public-key-file /tmp/id_ed25519_pi_tunnel.pub \
  --user pi-tunnel \
  --remote-ssh-port 2222 \
  --remote-vnc-port 5901
```

На Raspberry Pi после подготовки SSH key и `known_hosts`:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_reverse_ssh.sh \
  --server-host <SERVER_IP> \
  --server-user pi-tunnel \
  --identity-file /path/to/id_ed25519 \
  --known-hosts /path/to/known_hosts \
  --remote-ssh-port 2222 \
  --enable-vnc \
  --remote-vnc-port 5901
```

Подключение через fallback с сервера:

```bash
ssh -p 2222 RPI_USER@127.0.0.1
```

VNC через fallback:

```text
127.0.0.1:5901
```

## 11. Финальная проверка

После настройки и reboot Raspberry Pi нормальное состояние такое:

```bash
systemctl is-active host-monitor
systemctl is-active amneziawg-client@awg0
systemctl is-active reverse-ssh.service
ip addr show awg0
sudo awg show awg0
```

Ожидаемо:

- `host-monitor` — `active`.
- `amneziawg-client@awg0` — `active`.
- `reverse-ssh.service` — `active`, если fallback включён.
- `awg0` имеет VPN-адрес Raspberry Pi.
- В `sudo awg show awg0` есть свежий `latest handshake`.

Проверка прямого доступа с ноутбука:

```powershell
ping RPI_VPN_IP
ssh RPI_USER@RPI_VPN_IP
```

Проверка fallback:

```bash
ssh root@<SERVER_IP>
ssh -p 2222 RPI_USER@127.0.0.1
```

Проверка, что reverse-порты на сервере только локальные:

```bash
ss -ltn | grep -E '127.0.0.1:(2222|5901)'
```

## 12. Откат на Raspberry Pi

Отключить AmneziaWG unit:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/uninstall_remote_access.sh --interface awg0
```

Отключить и удалить наши конфиги:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/uninstall_remote_access.sh \
  --interface awg0 \
  --purge-config \
  --purge-reverse-ssh
```

Основной `host-monitor.service` этот откат не трогает.
