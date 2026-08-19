# Переход с SIM7600 GNSS на HGLRC M100 Mini

Целевой GPS-приёмник проекта — HGLRC M100 Mini (u-blox M10), подключённый
непосредственно к UART Raspberry Pi 4. Интернет после удаления SIM7600 работает
независимо от GPS.

## Подключение

Полностью обесточить Raspberry Pi перед монтажом.

| HGLRC M100 Mini | Raspberry Pi 40-pin |
| --- | --- |
| `5V` | pin 2 или pin 4 (`5V`) |
| `GND` | pin 6 (`GND`) |
| `TX` | pin 10 (`GPIO15/RXD0`) |
| `RX` | pin 8 (`GPIO14/TXD0`) |

`TX` и `RX` соединяются крест-накрест. PPS к проекту не подключается.

GPIO Raspberry Pi работают с логическими уровнями 3.3 В и не допускают 5 В на
входе. До подключения `TX` GPS к pin 10 измерить мультиметром напряжение
`TX-GND` включённого M100. Если оно выше 3.3 В, установить преобразователь
уровней или делитель на линии `M100 TX -> Pi RX`. Питание модуля и логический
уровень UART — разные параметры.

Антенна должна смотреть к небу. Не ставить модуль вплотную к DC/DC, USB-модему,
Wi-Fi-антенне, силовым проводам и металлу. Для первого холодного старта проверять
GPS на улице.

## UART Raspberry Pi OS

```bash
sudo raspi-config
```

В `Interface Options -> Serial Port`:

- login shell over serial: `No`;
- serial port hardware: `Yes`.

После перезагрузки:

```bash
sudo reboot
readlink -f /dev/serial0
systemctl list-units 'serial-getty@*' --all
```

Никакой `serial-getty` не должен владеть устройством, на которое указывает
`/dev/serial0`. Если такой unit остался, отключить его по фактическому имени,
например:

```bash
sudo systemctl disable --now serial-getty@ttyS0.service
```

Для production на Raspberry Pi 4 предпочтителен полноценный PL011 вместо
mini-UART. Если встроенный Bluetooth проекту не нужен, добавить в
`/boot/firmware/config.txt` (старые образы используют `/boot/config.txt`):

```ini
enable_uart=1
dtoverlay=disable-bt
```

Затем выполнить:

```bash
sudo systemctl disable --now hciuart
sudo reboot
readlink -f /dev/serial0
```

После этого на Pi 4 ожидается `/dev/ttyAMA0`. Сам проект продолжает использовать
стабильный алиас `/dev/serial0`, поэтому имя конкретного UART в конфиг не
зашивается.

В проекте используется фиксированный `/dev/serial0` и 115200 бод. Фиксированный
порт намеренно не переключается на случайные `/dev/ttyUSB*`, если GPS пропал.

## Удаление оставшегося управления SIM7600

В `config.yaml` должны быть одновременно выставлены:

```yaml
gps:
  port: "/dev/serial0"
  port_candidates: []
  baud: 115200
  max_fix_age_s: 1.0
  max_serial_backlog_bytes: 4096
  validate_source_time: false

lte:
  enabled: false
  events_enabled: false
```

`validate_source_time: false` нужен для холодного старта Raspberry Pi без NTP:
свежесть прямого UART-потока всё равно проверяется монотонными часами, но неверные
системные дата и время не блокируют настоящий fix.

Если на устройстве раньше устанавливались SIMCom watchdog и диагностика, их надо
отключить до демонтажа модема:

```bash
sudo systemctl disable --now simcom-ppp-watchdog.timer
sudo systemctl disable --now simcom-diagnostics.timer
sudo systemctl reset-failed simcom-ppp-watchdog.service simcom-diagnostics.service
```

Для полного, но обратимого удаления установленных файлов старого runtime есть
скрипт. Перед удалением он складывает найденные файлы в
`/var/backups/korovki-retired-simcom/<дата-время>`:

```bash
sudo bash ./systemd/retire-simcom.sh
```

Скрипт не сбрасывает USB hub и не отключает питание USB: новый LTE/Wi-Fi роутер
остаётся включённым.

Особенно важен `simcom-ppp-watchdog`: в опциональном режиме он умеет отключать
питание всех внешних USB-портов Raspberry Pi 4. Оставлять его после замены
интернета опасно для нового USB-сетевого устройства.

Старый `lte.service` отключать только если он всё ещё запускает SIM7600 PPP
(`pppd call megafon`). Если имя сервиса уже используется новым интернетом, его
трогать нельзя. Проверка:

```bash
systemctl cat lte.service
systemctl status lte.service --no-pager
```

Отключение старого PPP после проверки:

```bash
sudo systemctl disable --now lte.service
```

После отключения modem events SMS-команда `/reboot`, звонки, SMS, SIMCom RSSI и
температура исчезнут. Серверный контракт не меняется: `lte_rssi_dbm` станет `0`,
`lte_access_tech` — `"0"`, а `events_reader_ok` останется `true`, поскольку reader
явно отключён.

## Проверка до запуска host-monitor

Остановить сервис, чтобы он не конкурировал за UART:

```bash
sudo systemctl stop host-monitor
sudo stty -F /dev/serial0 115200 raw -echo
timeout 10 cat /dev/serial0
```

Ожидаются NMEA-строки `$GNGGA`, `$GNRMC` или совместимые `$GPGGA`/`$GPRMC`.
Штатный M10 поддерживает NMEA, а проект принимает префиксы разных созвездий.

Если виден бинарный поток или нет строк:

1. проверить перекрёстное подключение TX/RX и общий GND;
2. проверить 115200, затем 9600/38400/57600;
3. проверить модуль на улице и индикацию PPS;
4. при бинарном UBX включить вывод NMEA GGA и RMC через u-center/ubxtool и
   сохранить конфигурацию приёмника.

HGLRC M100 может выдавать UBX и NMEA одновременно. Reader умеет извлекать NMEA
из смешанного потока, однако для диагностики через gpsd следует включать режим
без записи в приёмник:

```bash
sudo gpsd -b -n /dev/serial0
```

Без `-b` gpsd имеет право перенастроить u-blox. Raspberry Pi сохраняет питание
5V во время обычного `sudo reboot`, поэтому временная конфигурация GPS может
пережить мягкую перезагрузку и сброситься только после полного снятия питания.

## Проверка проекта

```bash
cd /opt/host-monitor
source .venv/bin/activate
python -m unittest discover -s tests -v
sudo systemctl restart host-monitor
sleep 5
journalctl -u host-monitor -n 100 --no-pager
```

В журнале сначала должна появиться строка вида:

```text
GPS source detected: port=/dev/serial0 baud=115200
```

После 3D fix в статусе ожидаются `gps_fix=1` и малые значения `age_s`/`fix_age_s`.
Проверить фактическую телеметрию на сервере: ненулевые `lat`, `lon`,
`gps_valid=true`, адекватные `gps_satellites` и `speed_kmh`.

## Основные риски

- 5-вольтовый UART повредит GPIO Raspberry Pi — уровень TX измеряется заранее.
- Serial console/getty может одновременно читать порт и ломать NMEA-поток.
- Некоторые экземпляры M100 могут быть перенастроены на UBX-only или другую
  скорость; проект читает NMEA.
- Плохое место установки даёт fix на столе, но срывы рядом с работающим Wi-Fi,
  DC/DC или металлической крышей.
- Без компаса направление корректно только при движении; текущий проект компас
  нигде не использует.
- После удаления SIM7600 больше нет SMS-reboot. Нужен другой независимый канал
  восстановления, если это production-требование.
