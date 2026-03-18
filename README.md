# host-monitor (Raspberry Pi)

Сервис для Raspberry Pi, который **2 раза в секунду** собирает телеметрию (UTC, GPS, вес, Wi‑Fi клиенты, LTE метрики, статусы потоков) и отправляет её на сервер по **HTTP POST (JSON)**.  
Если интернета нет — данные **буферизуются в SQLite** и догружаются при восстановлении связи.

## Быстрый старт (на Raspberry Pi OS)

Установить Python зависимости:

```bash
sudo apt update
sudo apt install -y python3 python3-pip
python3 -m pip install -U pip
python3 -m pip install .
```

Настроить `config.yaml` (URL сервера, device_id, порты и т.д.).

Запуск в терминале:

```bash
host-monitor --config ./config.yaml
```

Калибровка веса (когда будет АЦП/тензо):

```bash
host-monitor-calibrate --config ./config.yaml tare
host-monitor-calibrate --config ./config.yaml calibrate --known-kg 100
```

## Автозапуск (systemd)

См. `systemd/host-monitor.service`.

