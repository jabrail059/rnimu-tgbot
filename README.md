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


## Контент и админка

Контент теперь хранится в SQLite и управляется из Mini App. Структура: разделы → материалы → текст и фотографии. Фото лежат в `MEDIA_DIR` отдельно от SQLite и не публикуются через StaticFiles.

Добавьте Telegram ID администратора в `ADMIN_USER_IDS` через запятую. После перезапуска бот создаст/обновит список администраторов. Администратор получает кнопку «Админка» в Mini App и может создавать/удалять разделы, создавать и редактировать материалы, загружать и удалять фотографии.

Пользовательская часть выдаёт список разделов только при активной подписке, затем материалы выбранного раздела, текст и галерею фотографий. Фото запрашиваются отдельным авторизованным API-запросом и рисуются в Canvas; прямых публичных URL к медиа нет.

Для загрузки файлов используется лимит 12 МБ на изображение. Поддерживаются PNG, JPG и WEBP.

### Деплой обновления

`data/app.db` не следует заменять архивом при обновлении production: это рабочая база с пользователями и платежами. Код при старте сам создаёт недостающие таблицы. Каталог `data/media` также должен оставаться на сервере.

После обновления исходников:
1. проверьте `.env` (`ADMIN_USER_IDS`, `MEDIA_DIR`, YooKassa);
2. перезапустите systemd-сервис;
3. откройте `/healthz`;
4. войдите в Mini App под Telegram-аккаунтом администратора и создайте первый раздел/материал.

### Что входит в обновлённый архив

В архив перенесены актуальные изменения VDS для `app/database.py` и `web/index.html`, после чего поверх них добавлены контент/админка, защищённая галерея и меню бота.

Файл `data/app.db` намеренно не включён в release-архив: на production нужно сохранить существующую VDS-базу. При старте приложение автоматически создаёт недостающие таблицы для разделов, материалов, изображений и администраторов.
