# Server setup notes

Этот файл описывает переносимый порядок подготовки сервера. Реальные IP, пароли и ключи здесь не храним.

## 1. Перенос сайта на новый сервер

Одного git-репозитория малины для переноса сайта недостаточно. Код Raspberry Pi и код сайта/backend — разные части системы.

Для переноса сайта нужны как минимум:

- репозиторий сайта/backend;
- переменные окружения backend;
- база данных, если она есть;
- файлы загрузок/статические данные, если они есть;
- `nginx` конфиг;
- `pm2`/systemd конфиг запуска backend;
- firewall rules;
- DNS/HTTPS настройки, если появятся.

## 2. Перед установкой Amnezia

1. Снять аудит по `SERVER_AUDIT_TEMPLATE.md`.
2. Создать backup.
3. Проверить сайт локально и снаружи.
4. Выбрать свободный UDP-порт для AmneziaWG, например `1234/udp`.
5. Убедиться, что этот порт не занят.

## 3. Установка Amnezia

Self-hosted Amnezia ставится через AmneziaVPN на ноутбуке по SSH-доступу к серверу.

После установки:

1. Открыть настройки серверного подключения.
2. Выбрать `AmneziaWG`.
3. Выставить выбранный UDP-порт.
4. Проверить, что сайт и backend продолжают отвечать.

## 4. Reverse SSH fallback на сервере

Если прямой client-to-client доступ через AmneziaWG не заработает, нужен отдельный tunnel user.

Рекомендуемая модель:

```text
User: pi-tunnel
Auth: SSH key only
Password login: disabled for this user
Remote listens: 127.0.0.1 only
GatewayPorts: no
```

Порты:

```text
127.0.0.1:2222 -> Raspberry Pi SSH
127.0.0.1:5901 -> Raspberry Pi VNC
```

Не открывай `2222` и `5901` наружу через firewall.

Подготовить сервер можно скриптом:

```bash
sudo bash ./remote_access/amnezia/prepare_reverse_ssh_server.sh \
  --public-key-file /path/to/id_ed25519_pi_tunnel.pub \
  --user pi-tunnel \
  --remote-ssh-port 2222 \
  --remote-vnc-port 5901
```

Проверить, что сервер принимает reverse forwarding:

```bash
ssh -i /path/to/id_ed25519_pi_tunnel \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/path/to/known_hosts \
  -N \
  -R 127.0.0.1:2222:127.0.0.1:22 \
  -p <SSH_PORT> \
  pi-tunnel@<SERVER_IP>
```
