# Патанатомия: Telegram bot + Mini App

Подписка на 30 дней. Контент выдаётся API только после серверной проверки подписанной Telegram `initData` и активной подписки.

## Платежи

Основной поток — YooKassa API v3. Бот создаёт redirect-платёж с `capture=true`; checkout ЮKassa показывает все включённые в кабинете методы (в том числе карту и СБП). Браузерное возвращение с checkout **не** выдаёт доступ. Только `POST /api/payment/webhook`, сверенный с `GET /payments/{id}` в YooKassa, меняет статус и продлевает подписку.

Временный Quickpay оставлен за `ENABLE_LEGACY_YOOMONEY=true`. Не включайте его после приёмочного теста YooKassa. В личном кабинете YooKassa настройте HTTPS URL `https://<домен>/api/payment/webhook` для событий `payment.succeeded`, `payment.waiting_for_capture`, `payment.canceled`.

## Локальный запуск

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
# заполните .env настоящими значениями
python main.py
```

`PUBLIC_BASE_URL` обязан быть публичным HTTPS URL. Для разработки используйте HTTPS-туннель. Не коммитьте `.env`, секрет YooKassa или токен Telegram.

## Ubuntu deployment

1. Создайте системного пользователя `pathology`, разверните проект в `/opt/rnimu-tgbot`, создайте venv и каталог `data`, принадлежащий этому пользователю.
2. Скопируйте `.env.example` в `/etc/pathology-bot.env`, укажите реальные секреты и установите права `sudo chmod 600 /etc/pathology-bot.env`.
3. Замените домен и пути сертификатов в `deploy/nginx.conf`, включите конфигурацию nginx и получите TLS-сертификат.
4. Установите `deploy/pathology-bot.service` в `/etc/systemd/system/`, затем выполните:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pathology-bot
sudo nginx -t && sudo systemctl reload nginx
sudo journalctl -u pathology-bot -f
```

For consistent SQLite backups (including WAL data), install and enable the backup timer:

```bash
sudo install -d -o pathology -g pathology -m 700 /var/backups/pathology-bot
sudo cp deploy/pathology-backup.service deploy/pathology-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pathology-backup.timer
```

The template retains 14 daily files. Copy backups off-host; a backup on the same disk does not protect against disk loss.

Не открывайте порт 8000 наружу: сервис слушает только `127.0.0.1`. `TRUST_PROXY_HEADERS=true` допустим только с приложенной конфигурацией nginx и таким сетевым ограничением.

## Перед production

- Проверьте `https://<домен>/healthz`.
- В YooKassa включите методы оплаты (карта, СБП и нужные дополнительные) и укажите webhook URL/события.
- Проведите тестовый платёж: в БД должна появиться запись `provider=yookassa`, затем `succeeded`; подписка должна продлиться ровно один раз при повторной доставке webhook.
- Проверьте, что возврат со страницы оплаты без `payment.succeeded` не открывает контент.
- Проверьте `/start`, покупку, истечение подписки, а также логи `journalctl`.
- После приёмки установите `ENABLE_LEGACY_YOOMONEY=false` и удалите legacy-ключи из `/etc/pathology-bot.env`.
