# host-monitor (Raspberry Pi)

English version: [README.md](./README.md)

Клиент телеметрии для Raspberry Pi 4 + SIM7600 + Waveshare ADS1263.

- Отправляет телеметрию по HTTP POST в JSON
- Складывает неотправленные пакеты и события модема в SQLite при пропадании сети
- Читает GPS с последовательных портов модема с автоопределением
- Читает SMS и звонки с AT-порта модема и отправляет их на отдельный API
- Читает вес через пассивное параллельное подключение к существующей тензосистеме

## 1) Формат телеметрии

Текущий пакет телеметрии:

```json
{
  "device_id": "isrk_hozyain_01",
  "timestamp": "2026-03-11T20:34:38",
  "lat": 55.109311,
  "lon": 82.812417,
  "gps_valid": true,
  "gps_satellites": 12,
  "gps_age_s": 0.2,
  "speed_kmh": 18.52,
  "weight": 1234.56,
  "raw": 1236.78,
  "weight_valid": true,
  "gps_quality": 1,
  "wifi_clients": ["aa:bb:cc:dd:ee:ff"],
  "cpu_temp_c": 61.2,
  "lte_rssi_dbm": -75,
  "lte_access_tech": "LTE",
  "events_reader_ok": true
}
```

Что означают флаги состояния:

- `gps_valid`: `true`, когда текущие координаты получены из валидного GPS fix
- `gps_age_s`: возраст последнего GPS fix с учётом UTC-времени внутри NMEA
- `speed_kmh`: скорость по GPS в км/ч, берется из NMEA RMC; `0.0`, если валидного GPS fix нет
- `weight`: сглаженный вес в кг после медианы/EMA-фильтра
- `raw`: откалиброванный вес в кг до медианы и сглаживания; нужен для диагностики и фильтрации на сервере
- `weight_valid`: `true`, когда текущее значение веса считано без ошибки
- `events_reader_ok`: `true`, когда reader событий модема работает нормально, либо когда события модема отключены в конфиге

SMS и звонки уходят на отдельный endpoint `events.url`:

```json
{
  "device_id": "isrk_hozyain_01",
  "type": "sms",
  "timestamp": "2026-03-11T20:35:01",
  "from": "+79991234567",
  "text": "hello"
}
```

Пример пакета о звонке:

```json
{
  "device_id": "isrk_hozyain_01",
  "type": "call",
  "timestamp": "2026-03-11T20:35:10",
  "from": "+79991234567",
  "text": ""
}
```

## 2) Установка на Raspberry Pi OS

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git hostapd modemmanager sqlite3
```

Включить SPI для ADS1263:

```bash
sudo raspi-config
# Interface Options -> SPI -> Enable
sudo reboot
```

Клонирование проекта и установка в виртуальное окружение:

```bash
sudo mkdir -p /opt
cd /opt
sudo git clone https://github.com/idkotin/malina_for_korovki.git host-monitor
sudo chown -R $USER:$USER /opt/host-monitor
cd /opt/host-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install .
```

Ручной запуск через Python из `venv`:

```bash
/opt/host-monitor/.venv/bin/python -m host_monitor.main --config /opt/host-monitor/config.yaml
```

Установка библиотеки Waveshare для ADS1263:

```bash
cd /opt
git clone https://github.com/waveshareteam/High-Pricision_AD_HAT.git
```

Путь к библиотеке в `config.yaml`:

```yaml
weight:
  waveshare_path: "/opt/High-Pricision_AD_HAT/python"
```

## 3) Настройка

Отредактируй `/opt/host-monitor/config.yaml`.

Важные поля:

- `send.url`: URL API для телеметрии
- `events.url`: URL API для SMS и звонков
- `send.interval_s`: период формирования и немедленной попытки отправки свежей телеметрии
- `send.max_batch`: максимум записей в одном запросе; пачка не ждёт заполнения
- `send.idle_sleep_enabled`: замедлять отправку телеметрии, когда машина стоит
- `send.idle_after_s`: сколько секунд без подтвержденного движения ждать до замедленной отправки
- `send.idle_interval_s`: период отправки телеметрии в спящем режиме
- `send.movement_confirm_s`: сколько секунд должно длиться движение для возврата к обычной отправке
- `send.movement_speed_kmh`: порог скорости GPS для подтверждения движения
- `wifi.scan_interval_s`: период фонового опроса подключённых Wi-Fi-клиентов
- `wifi.max_snapshot_age_s`: максимальный возраст списка клиентов; более старый список считается пустым
- `gps.port`: фиксированный GPS-порт (`null` для автоопределения)
- `gps.port_candidates`: кандидаты GPS-портов
- `gps.max_fix_age_s`: максимальный возраст fix; более старые координаты отправляются как невалидные, а не переиспользуются
- `lte.at_ports`: кандидаты AT-портов
- `weight.enabled`: включить или выключить чтение веса
- `weight.simulate`: использовать фейковый вес вместо реального АЦП
- `weight.ref_pos` / `weight.ref_neg`: пара входов для опорного напряжения
- `weight.channel_pos` / `weight.channel_neg`: пара входов для сигнала моста

Флаги состояния в телеметрии:

- `gps_valid`: валиден ли текущий GPS fix
- `gps_age_s`: возраст этого fix в секундах при создании пакета
- `weight_valid`: валидно ли текущее значение веса
- `events_reader_ok`: в порядке ли сейчас reader событий модема

Конфиг по умолчанию для тензо:

```yaml
weight:
  ref_pos: 0
  ref_neg: 1
  channel_pos: 2
  channel_neg: 3
```

Спящий режим при простое замедляет формирование telemetry-пакетов. GPS, вес и события модема продолжают работать в обычном цикле. В штатном production-конфиге спящий режим выключен.

Каждый свежий пакет сначала сохраняется в SQLite outbox и сразу будит единственный telemetry-отправитель. При пустом хвосте запрос содержит одну запись; при наличии хвоста — свежую запись и до `send.max_batch - 1` самых старых. Строки удаляются только по `acked_packet_ids` сервера. Чтение веса и опрос Wi-Fi остаются отдельными фоновыми задачами, поэтому медленный ADS1263 не уменьшает заданную частоту телеметрии.

## 4) Интеграция в существующую тензосистему

Проект рассчитан на пассивное параллельное подключение к уже существующей системе взвешивания сельхозмашины.

- Штатный терминал взвешивания остается подключенным и продолжает питать мост
- Этот проект не заменяет штатный терминал
- Этот проект не подает питание на тензомост с платы ADS1263
- ADS1263 только считывает существующее возбуждение моста и сигнал с него

Исходные условия:

- На машине уже есть 3 тензодатчика, подключенные через сумматорную коробку
- Мы врезаемся после сумматора, параллельно штатному терминалу
- Доступные линии: `E+`, `E-`, `SIG+`, `SIG-`, `shield/drain`
- Ожидается, что питание моста дает штатный терминал

Подключение от выхода сумматора к ADS1263 HAT:

- `E+` -> `IN0`
- `E-` -> `IN1`
- `SIG+` -> `IN2`
- `SIG-` -> `IN3`
- `shield/drain` -> оставлять как экран; не использовать как сигнальный провод

Как работает АЦП в этом проекте:

- Дифференциальное измерение идет по `IN2 - IN3`
- Внешняя дифференциальная опора задается по `IN0 - IN1`
- `E+ / E-` используются только как sense reference
- АЦП не питает мост
- Это не схема в стиле HX711, где АЦП сам возбуждает мост

Замечание по экрану и земле:

- Нельзя использовать экран вместо `SIG-` или `E-`
- Подключать экран к локальной земле стоит только если это согласовано со штатной схемой машины и не создает ground loop

## 5) Ручной запуск

```bash
cd /opt/host-monitor
source .venv/bin/activate
host-monitor --config ./config.yaml
```

Без активации окружения:

```bash
/opt/host-monitor/.venv/bin/host-monitor --config ./config.yaml
```

## 6) Запуск через systemd

```bash
sudo cp /opt/host-monitor/systemd/host-monitor.service /etc/systemd/system/host-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now host-monitor
sudo systemctl status host-monitor
```

Перезапуск и остановка:

```bash
sudo systemctl restart host-monitor
sudo systemctl stop host-monitor
```

## 7) Логи

Логи сервиса:

```bash
journalctl -u host-monitor -f
```

Логи в файле:

```bash
tail -f /opt/host-monitor/logs/host_monitor.log
```

Тот же AT-reader раз в 30 секунд опрашивает температуру и напряжение
самого SIM7600. Посмотреть только эти строки:

```bash
sudo journalctl -u host-monitor.service -f -o short-iso | grep --line-buffered 'modem health:'
```

Нельзя параллельно открывать AT-порт через `minicom`, `screen` или второй
Python-процесс. Второй reader может смешать AT-ответы и забрать SMS URC
у `host-monitor`.

## 8) Удаленный доступ

Файлы для удаленного доступа лежат в [`remote_access/amnezia`](./remote_access/amnezia). Не коммить реальные IP, пароли, приватные ключи, экспортированные `.conf` из Amnezia и локальные `known_hosts`. В `.gitignore` уже добавлены правила для типовых секретов в этой папке.

Проверенное целевое состояние:

- `host-monitor.service` включен и стартует после перезагрузки Raspberry Pi.
- `amneziawg-client@awg0.service` включен и стартует после перезагрузки Raspberry Pi.
- Установленный AmneziaWG-конфиг работает в service-only режиме: `AllowedIPs` переписывается в `<VPN_CIDR>`, а не остается `0.0.0.0/0`.
- Пустые опциональные поля из экспортированного AmneziaWG-конфига, например `I2 =`, пропускаются при установке.
- `reverse-ssh.service` — необязательный запасной доступ; на сервере он слушает только `127.0.0.1`.

### Способ A: прямой доступ через AmneziaWG

В AmneziaVPN на ноутбуке:

- Добавь или открой self-hosted сервер.
- Выбери протокол `AmneziaWG` и поставь UDP-порт сервера, например `1234`.
- Создай отдельный доступ для ноутбука, например `admin-laptop`, в формате для приложения AmneziaVPN.
- Создай отдельный доступ для Raspberry Pi, например `raspberry-pi`, в формате `AmneziaWG native config`.
- Сохрани экспорт для Raspberry Pi как `amnezia_for_awg.conf` и скопируй его на малину в `/opt/host-monitor/remote_access/amnezia/amnezia_for_awg.conf`.

Установка AmneziaWG-клиента на Raspberry Pi:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_amnezia_client.sh \
  --config ./remote_access/amnezia/amnezia_for_awg.conf \
  --interface awg0 \
  --service-allowed-ips <VPN_CIDR> \
  --install-packages
```

`<VPN_CIDR>` — внутренняя сеть Amnezia, которую малина должна видеть через VPN, например `10.8.1.0/24`. Не используй `0.0.0.0/0`, если специально не хочешь пустить весь интернет малины через VPN.

Проверка VPN-сервиса:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/check_remote_access.sh --interface awg0
sudo systemctl status amneziawg-client@awg0 --no-pager
sudo awg show awg0
ip addr show awg0
```

Подключение с ноутбука, когда ноутбук и малина подключены к AmneziaWG:

```powershell
ping <RPI_VPN_IP>
ssh <RPI_USER>@<RPI_VPN_IP>
```

Если на Raspberry Pi включен VNC, подключай VNC-клиент к:

```text
<RPI_VPN_IP>:5900
```

### Способ B: reverse SSH fallback

Этот способ нужен, если ноутбук и Raspberry Pi оба подключены к AmneziaWG, но прямой client-to-client доступ не работает.

Создать ключ туннеля на Raspberry Pi:

```bash
mkdir -p ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/korovki_pi_tunnel -N ""
ssh-keyscan -p 22 <SERVER_IP> > ~/.ssh/korovki_server_known_hosts
scp ~/.ssh/korovki_pi_tunnel.pub root@<SERVER_IP>:/tmp/id_ed25519_pi_tunnel.pub
```

Подготовить ограниченного пользователя для туннеля на сервере:

```bash
if [ -d /opt/host-monitor-remote-tools/.git ]; then
  cd /opt/host-monitor-remote-tools
  git pull
else
  git clone https://github.com/idkotin/malina_for_korovki.git /opt/host-monitor-remote-tools
  cd /opt/host-monitor-remote-tools
fi
sudo bash ./remote_access/amnezia/prepare_reverse_ssh_server.sh \
  --public-key-file /tmp/id_ed25519_pi_tunnel.pub \
  --user pi-tunnel \
  --remote-ssh-port 2222 \
  --remote-vnc-port 5901
```

Включить reverse tunnel service на Raspberry Pi:

```bash
cd /opt/host-monitor
sudo bash ./remote_access/amnezia/install_reverse_ssh.sh \
  --server-host <SERVER_IP> \
  --server-user pi-tunnel \
  --identity-file ~/.ssh/korovki_pi_tunnel \
  --known-hosts ~/.ssh/korovki_server_known_hosts \
  --remote-ssh-port 2222 \
  --enable-vnc \
  --remote-vnc-port 5901 \
  --install-packages
```

Проверка автозапуска fallback:

```bash
sudo systemctl status reverse-ssh.service --no-pager
sudo journalctl -u reverse-ssh.service -n 100 --no-pager
```

Подключение через сервер:

```powershell
ssh -J root@<SERVER_IP> -p 2222 <RPI_USER>@127.0.0.1
```

Или сначала зайти на сервер, а потом подключиться к локальному reverse-порту:

```bash
ssh root@<SERVER_IP>
ssh -p 2222 <RPI_USER>@127.0.0.1
```

Для VNC через fallback открой локальный туннель с ноутбука:

```powershell
ssh -L 5901:127.0.0.1:5901 root@<SERVER_IP>
```

Потом подключай VNC-клиент к:

```text
127.0.0.1:5901
```

## 9) Просмотр и очистка буфера

Если `sqlite3` еще не установлен:

```bash
sudo apt install -y sqlite3
```

Показать, сколько сейчас строк лежит в буфере телеметрии и событий:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "select 'telemetry' as table_name, count(*) as rows from telemetry union all select 'events', count(*) from events;"
```

Показать последние буферизованные пакеты телеметрии:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "select id, created_utc, substr(payload_json,1,200) from telemetry order by id desc limit 10;"
```

Показать последние буферизованные события модема:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "select id, created_utc, substr(payload_json,1,200) from events order by id desc limit 10;"
```

Очистить только буфер телеметрии:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "delete from telemetry;"
```

Очистить только буфер событий модема:

```bash
cd /opt/host-monitor
sqlite3 ./data/buffer.sqlite3 "delete from events;"
```

Полностью удалить базу буфера:

```bash
cd /opt/host-monitor
rm -f ./data/buffer.sqlite3 ./data/buffer.sqlite3-shm ./data/buffer.sqlite3-wal
```

## 10) Калибровка

Перед калибровкой:

- `weight.enabled: true`
- `weight.simulate: false`
- Врезка в тензолинию подключена правильно
- Штатный терминал включен и возбуждает мост

Команда:

```bash
cd /opt/host-monitor
source .venv/bin/activate
host-monitor-calibrate --config ./config.yaml
```

Порядок действий:

- Положи первый известный груз и дождись стабилизации показаний
- Когда скрипт попросит первую точку, введи текущий известный общий вес в кг. `0` допустим, если первая точка снимается на пустой машине
- Затем добавь или измени груз и снова дождись стабилизации
- Нажми Enter, когда скрипт попросит продолжить, и введи новый известный общий вес в кг
- Скрипт сам посчитает и сохранит `offset` и `scale` по двум измеренным точкам

Калибровка сохраняется в:

- `weight.calibration_path` (по умолчанию `./data/scale_calibration.json`)

## 11) Если тензолиния еще не подключена

Укажи в конфиге:

```yaml
weight:
  enabled: false
```

Сервис продолжит нормально работать. В телеметрии поле `weight` будет отправляться как `0.0`, а `weight_valid` будет `false`.

## 12) Ручная и защищённая автоматическая перезагрузка

Ручная перезагрузка по SMS по умолчанию выключена. В живом конфиге Raspberry
Pi она ограничивается одним номером и точной командой `/reboot`:

```yaml
sms_reboot:
  enabled: true
  allowed_number: "+7XXXXXXXXXX"
  command: "/reboot"
```

Защищённая автоматическая перезагрузка также по умолчанию выключена:

```yaml
auto_reboot:
  enabled: true
  telemetry_inactive_s: 900.0
  terminal_off_below_raw_kg: -1000.0
  terminal_off_confirm_s: 30.0
  max_weight_age_s: 10.0
  healthy_success_max_age_s: 10.0
  healthy_reset_confirm_s: 60.0
  state_path: "./data/auto_reboot_state.json"
```

Автоматический reboot разрешён только при одновременном выполнении условий:

- сервер не подтверждал ни одного host-пакета не менее 15 минут;
- свежий нефильтрованный `raw` веса строго меньше `-1000 кг` не менее 30
  секунд.

Отсутствующий или устаревший вес, ровно `-1000 кг`, либо продолжающиеся HTTP
ACK полностью блокируют reboot. Используется именно `raw`: обычная телеметрия
считает такой вес невалидным, но в этой аппаратной установке он означает, что
штатный весовой терминал выключен.

Перед reboot на диск записывается защёлка. Повторная перезагрузка при той же
аварии невозможна; защёлка снимается только после 60 секунд устойчивых ACK.
Watchdog работает внутри `host-monitor`, поэтому зависший процесс не имеет права
перезагружать машину по старому значению веса. Подробный разбор инцидента и
предупреждение про PPP находятся в
[`INCIDENT_2026-07-19_20.md`](./INCIDENT_2026-07-19_20.md).
