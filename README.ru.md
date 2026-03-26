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
  "weight": 1234.56,
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
sudo apt install -y python3 python3-pip python3-venv git hostapd modemmanager
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
- `send.interval_s`: период отправки телеметрии
- `gps.port`: фиксированный GPS-порт (`null` для автоопределения)
- `gps.port_candidates`: кандидаты GPS-портов
- `lte.at_ports`: кандидаты AT-портов
- `weight.enabled`: включить или выключить чтение веса
- `weight.simulate`: использовать фейковый вес вместо реального АЦП
- `weight.ref_pos` / `weight.ref_neg`: пара входов для опорного напряжения
- `weight.channel_pos` / `weight.channel_neg`: пара входов для сигнала моста

Флаги состояния в телеметрии:

- `gps_valid`: валиден ли текущий GPS fix
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

## 8) Калибровка

Перед калибровкой:

- `weight.enabled: true`
- `weight.simulate: false`
- Врезка в тензолинию подключена правильно
- Штатный терминал включен и возбуждает мост

Команды:

```bash
cd /opt/host-monitor
source .venv/bin/activate
host-monitor-calibrate --config ./config.yaml tare
host-monitor-calibrate --config ./config.yaml calibrate --known-kg 100
```

Калибровка сохраняется в:

- `weight.calibration_path` (по умолчанию `./data/scale_calibration.json`)

## 9) Если тензолиния еще не подключена

Укажи в конфиге:

```yaml
weight:
  enabled: false
```

Сервис продолжит нормально работать. В телеметрии поле `weight` будет отправляться как `0.0`, а `weight_valid` будет `false`.
