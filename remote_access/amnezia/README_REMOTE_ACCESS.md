# Remote access for Korovki Raspberry Pi

Этот каталог подготавливает безопасный удалённый доступ к Raspberry Pi через AmneziaWG, не меняя основной код проекта `host-monitor` и не трогая серверный сайт без отдельного подтверждения.

## Что уже подготовлено

- `install_amnezia_client.sh` — ставит конфиг AmneziaWG на Raspberry Pi, копирует systemd unit и включает автозапуск.
- `check_remote_access.sh` — показывает статус VPN, интерфейса, маршрутов и логов.
- `uninstall_remote_access.sh` — отключает только добавленные нами unit-файлы и, при желании, удаляет конфиги.
- `install_reverse_ssh.sh` — готовит резервный reverse SSH tunnel на случай, если client-to-client доступ через Amnezia не заработает.
- `prepare_reverse_ssh_server.sh` — готовит серверного пользователя для reverse SSH fallback.
- `systemd/amneziawg-client@.service` — unit для автозапуска `awg-quick`.
- `systemd/reverse-ssh.service` — unit для резервного туннеля.
- `run_reverse_ssh.sh` — helper для `autossh`.
- `SERVER_AUDIT_TEMPLATE.md` — шаблон аудита сервера перед установкой Amnezia.
- `RUNBOOK_RASPBERRY_PI.md` — пошаговая инструкция для AmneziaVPN на ноутбуке и запуска на Raspberry Pi.

## Что мы принципиально не делаем автоматически

- Не меняем `host-monitor.service`.
- Не меняем `config.yaml`.
- Не меняем APN, SIM, NetworkManager и основной маршрут без отдельного решения.
- Не принимаем вслепую full-tunnel конфиг с `AllowedIPs = 0.0.0.0/0` или `::/0`.
- Для малины используем service-only маршрут через `--service-allowed-ips`, если экспортированный Amnezia config full-tunnel.
- Не угадываем параметры AmneziaWG вручную: конфиг должен быть экспортирован из AmneziaVPN.

## Что ещё нужно от тебя

1. Данные сервера, когда он включится:
   - публичный IP;
   - root-пароль или SSH-доступ.
2. Экспортированный конфиг для Raspberry Pi:
   - открыть AmneziaVPN;
   - зайти в подключение к серверу;
   - открыть `Share VPN Access` / `Поделиться VPN-доступом`;
   - создать отдельного пользователя `raspberry-pi`;
   - выбрать протокол `AmneziaWG`;
   - выбрать формат `AmneziaWG native config`, если он доступен;
   - сохранить файл и передать его.
3. Позже, если понадобится прямой доступ с ноутбука:
   - создать отдельный доступ для `admin-laptop`;
   - импортировать его в AmneziaVPN на ноутбуке.

## Безопасный порядок работ

1. Сначала аудит сервера без изменений.
2. Потом выбор свободного UDP-порта для AmneziaWG.
3. Только после отдельного подтверждения — изменения на сервере.
4. После получения native-конфига — настройка Raspberry Pi.
5. Если прямой VPN-доступ между клиентами не заработает — включаем reverse SSH fallback.

## Установка клиента AmneziaWG на Raspberry Pi

Пример:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_amnezia_client.sh \
  --config /path/to/raspberry-pi.conf \
  --interface awg0 \
  --service-allowed-ips VPN_CIDR
```

Что делает скрипт:

- проверяет наличие `awg` / `awg-quick`;
- по запросу может попробовать поставить пакет `amneziawg` через `apt`;
- делает backup старого конфига и unit-файла;
- копирует конфиг в `/etc/amnezia/amneziawg/<interface>.conf`;
- ставит `amneziawg-client@.service`;
- включает `amneziawg-client@<interface>.service`.

Если в экспортированном конфиге есть `AllowedIPs = 0.0.0.0/0` или `::/0`, запускай установку с `--service-allowed-ips VPN_CIDR`. Скрипт установит копию конфига с заменённым `AllowedIPs` и не будет менять исходный экспортированный файл.

Если хочешь, чтобы скрипт сам попробовал поставить пакет `amneziawg`, добавь флаг:

```bash
--install-packages
```

Этот режим трогает `apt` и репозитории пакетов, поэтому его стоит запускать только после проверки ОС Raspberry Pi.

## Диагностика

Проверка статуса:

```bash
sudo bash ./remote_access/amnezia/check_remote_access.sh --interface awg0
```

Полезные команды:

```bash
sudo systemctl status amneziawg-client@awg0 --no-pager
sudo journalctl -u amneziawg-client@awg0 -n 100 --no-pager
sudo awg show awg0
ip addr show awg0
ip route show table all | grep awg0
```

## Reverse SSH fallback

Этот сценарий нужен только если:

- ноутбук и Raspberry Pi оба подключены к Amnezia, но не видят друг друга;
- или прямой доступ есть, но нестабилен.

Ожидаемое поведение:

- Raspberry Pi сама поднимает исходящее SSH-соединение на сервер;
- на сервере локально появляются порты:
  - `127.0.0.1:2222 -> Raspberry Pi:22`
  - `127.0.0.1:5901 -> Raspberry Pi:5900` при включённом VNC;
- наружу эти порты не публикуются.

Пример установки:

```bash
sudo bash ./remote_access/amnezia/install_reverse_ssh.sh \
  --server-host SERVER_IP \
  --server-user pi-tunnel \
  --identity-file /path/to/id_ed25519 \
  --known-hosts /path/to/known_hosts \
  --remote-ssh-port 2222 \
  --enable-vnc \
  --remote-vnc-port 5901
```

## Откат

Отключить только наши сервисы:

```bash
sudo bash ./remote_access/amnezia/uninstall_remote_access.sh --interface awg0
```

Если нужно ещё и удалить конфиги:

```bash
sudo bash ./remote_access/amnezia/uninstall_remote_access.sh \
  --interface awg0 \
  --purge-config \
  --purge-reverse-ssh
```

Скрипт удаления не трогает основной проект, Python-окружение, `host-monitor.service` и не удаляет пакет `amneziawg`.
