# Профили ADC1/ADC2 и одна точка

Подготовлено по прямому поручению пользователя. Развёртывание и результат
аппаратного self-test дописываются ниже после проверки.

ADC2 сохраняет internal reference, gain128, 100SPS и существующий файл
standalone-calibration.json. ADC1: internal reference, gain32, 100SPS, sinc3,
тот же вход IN0/IN1; отдельный файл standalone-calibration.json.adc1.json.
Выбор сохраняется в standalone-calibration.json.mode.json и переживает питание.
Переключение выполняет только sampler; новое измерение публикуется после него.
Исходный ADC2 calibrationId сохраняется при возвращении.

Чтение burst: START один раз на восемь результатов, каждый новый результат
проверяется по status/checksum, затем STOP. Нет старого результата после паузы.
Тайм-аут 0.5 с, неверная сумма/переполнение/сброс/ADC1 PGA alarm отклоняются.
Частота публикации остаётся 2 Гц; оценивать выигрыш чтения отдельно от отклика экрана.

## Кнопки (PIN задаются только в рабочем config)

- Вход: держать плюс и минус одновременно 3 секунды.
- Плюс/минус меняют цифру PIN, короткий NETT — следующая цифра.
- PIN 7229: выбор AdC 1 / AdC 2; плюс/минус переключают, NETT держать 2 секунды
  для применения. Power отменяет. Без калибровки ADC1 выдаёт прочерки, не выдуманный вес.
- PIN 7230: привязка ADC1 к одной известной массе. По умолчанию 0 кг, короткое
  плюс/минус меняет по 5 кг; удержание ускоряет по 50. Держать NETT 2 секунды.
  Дождаться завершения сбора, не менять нагрузку. При нестабильности Err без записи.
  Ноль вводить только на действительно пустом кузове; известную массу вводить как
  полный вес содержимого, не как вес добавленной порции поверх неизвестного остатка.
- PIN 7227: обычный захват пустого нуля, затем долгий NETT.
- PIN 7228: обычная калибровка по известной полной нагрузке после захвата нуля.
  Ввести массу и долгий NETT. Не перезапускать питание/службу и не переключать ADC
  между захватом нуля и span. Привязка 7230 к нулю также подготавливает этот span.

## Что означает одна точка

ADC2 raw = код24/gain128; ADC1 raw = код32/(256*gain32).
Оба выражаются в одинаковых номинальных единицах при одной внутренней опоре.
Поэтому стартовый slope берётся из сохранённой ADC2 калибровки, а собственный offset
ADC1 измеряется: offset = median(raw) - известные_кг/slope.
Это provisional=true/precision_verified=false. Одна точка НЕ определяет одновременно
и offset, и slope и не исправляет ошибку старого slope.
На отключённых датчиках никакую точку не снимать. Две точки позволяют определить
собственный slope ADC1. Возврат ADC2 восстанавливает его файл без перезаписи.

Опорное напряжение, питание/распайка моста и автоматическое обнуление не меняются.
Остановиться для фиксации точки, по возможности без мешалки; после этого отдельно
проверить рабочий режим с мешалкой. Не переключать профиль в активном компоненте
планшета: calibrationId изменится, смешивать начало/конец разных калибровок нельзя.

Self-test использует только внутренний TDAC, 0 и около +7.8 мВ, без вывода напряжений
на контакты. Служба должна быть остановлена, восстановление защищено таймером.
Он проверяет чтение/соотношение масштабов, но не точность массы в машине.

## ADC profiles and burst — 28.09.2026
User explicitly requested deployment, button switching and provisional ADC1 calibration by one known point. See ADC_PROFILES_2026-09-28.md. ADC2 calibration remains byte-identical. New live PIN7229 selects ADC1/2; PIN7230 anchors ADC1 to known total kg (zero allowed). ADC1 gain32/internal/100SPS/sinc3 has separate .adc1.json; persisted choice .mode.json. Old ADC2 slope is reused only as provisional; one point measures ADC1 offset, not gain. No automatic calibration on disconnected inputs. WeightSampler owns all switching/calibration; profiles change calibrationId, do not switch mid-component on tablet. Internal TDAC self-test passed: ADC2 old batch0.250s/new0.102s; ADC1 batch0.101s. This does not test bridge accuracy. Initial ADC1 estimate offset1791.3627929687502, scale2.0209731543624163, confirmed=false until field anchor. Burst enabled on ADC2. Backup /opt/host-monitor-backup-profiles-AnQMlc9f. Current disconnected bridge correctly invalid; input saturation should not reset chip continuously. 90 unit tests after this guard. Don't overwrite original ADC2 calibration or run old provisional-calibration script.
