# Media Downloader Bot

Самостоятельно размещаемый Telegram-бот, который скачивает видео, аудио и
фото-посты с популярных платформ в **максимальном качестве** и присылает их
прямо в чат. Рассчитан на работу даже в сетях с **DPI-ограничениями** (умеет
автоматически уводить заблокированные платформы через VLESS-прокси) и на загрузку
файлов до **2 ГБ** через собственный Bot API сервер.

> ▶️ **Готовый к работе бот: [@TikTokDownloaderFF_bot](https://t.me/TikTokDownloaderFF_bot)** —
> можно пользоваться прямо сейчас, ничего не разворачивая. /
> **Live bot: [@TikTokDownloaderFF_bot](https://t.me/TikTokDownloaderFF_bot)** — use it now, no setup needed.

**Языки:** [Русский](#-русская-версия) · [English](#-english-version)

---

## 🇷🇺 Русская версия

> Это инфраструктурный/автоматизационный проект. Свой токен бота, свой сервер и
> свои аккаунты вы подставляете сами; никаких учётных данных в репозитории нет.

### Содержание

- [Возможности](#возможности)
- [Как это работает](#как-это-работает)
- [Решённые сложные проблемы (подводные камни)](#решённые-сложные-проблемы-подводные-камни)
- [Требования](#требования)
- [Быстрый старт](#быстрый-старт)
- [Справочник по настройкам](#справочник-по-настройкам)
- [Как получить каждый кред самому](#как-получить-каждый-кред-самому)
- [Экспорт куки из браузера](#экспорт-куки-из-браузера)
- [Установка — обычный VPS](#установка--обычный-vps)
- [Установка — Proxmox LXC](#установка--proxmox-lxc)
- [VLESS-маршрутизация](#vless-маршрутизация)
- [Загрузки до 2 ГБ (свой Bot API)](#загрузки-до-2-гб-свой-bot-api)
- [Ограничение полосы](#ограничение-полосы)
- [Админ-панель и команды](#админ-панель-и-команды)
- [Структура проекта](#структура-проекта)
- [Решение проблем](#решение-проблем)

### Возможности

**Скачивание**

- **Много платформ** — YouTube (включая Shorts и YouTube Music), TikTok,
  Instagram, Twitter/X, SoundCloud, Spotify, Yandex Music и другие. Просто
  пришлите ссылку — платформа определяется автоматически.
- **Максимальное качество без перекодирования.** Видео всегда берётся в самом
  высоком разрешении/FPS и только *ремуксится* (не перекодируется) — без потери
  качества и без нагрузки на CPU. Среди форматов равного качества предпочитается
  **H.264/AAC**, потому что мобильный Telegram проигрывает другие кодеки
  (VP9/AV1) как застывший кадр со звуком.
- **Меню качества с размерами.** Для платформ с несколькими разрешениями бот
  показывает инлайн-меню (напр. *1080p · 320 МБ*, *720p · 140 МБ*), чтобы выбрать
  до скачивания.
- **Извлечение аудио.** У каждого видео есть кнопка *«Скачать аудио»*; ссылки
  YouTube Music / SoundCloud / Spotify / Yandex Music сразу отдаются аудиофайлом.
  Spotify и Yandex подбираются к источнику автоматически (поток Spotify защищён
  DRM и напрямую не качается).
- **Фото и карусели.** Фото-посты и карусели (в т.ч. смешанные фото+видео, плюс
  прикреплённая музыка) скачиваются и отправляются группой. Слишком большие или
  очень длинные/широкие картинки уходят документом, чтобы Telegram их не отверг.
- **Описание поста.** У постов с текстом появляется кнопка *«Описание»* для
  получения оригинального текста.
- **Кэш файлов по качеству.** `file_id` каждого загруженного файла хранится в
  PostgreSQL по каждому разрешению. Уже скачанная ранее ссылка **мгновенно
  переотправляется** без скачивания и перезаливки — и это переживает рестарты
  бота, пересборки образа и перезагрузки сервера.

**Надёжность и сеть**

- **Адаптивная маршрутизация через прокси.** Бот ходит напрямую, пока платформа
  доступна, и прозрачно переключается на VLESS-прокси, как только платформа
  заблокирована/гео-ограничена/забанена по IP — решение принимается отдельно по
  каждой платформе и постоянно перепроверяется. Часть платформ можно жёстко
  закрепить на «всегда через прокси».
- **Загрузки до 2 ГБ.** Опционально поднимается свой Telegram Bot API сервер,
  снимающий облачный лимит 50 МБ до 2 ГБ.
- **Ограничение полосы.** Хостовый шейпер ограничивает суммарную пропускную
  способность контейнера (в обе стороны), чтобы тяжёлая загрузка не забивала
  канал.
- **Самовосстановление.** Все сервисы под Docker с политиками рестарта; прокси
  сам обновляет список нод из подписки и перезагружается при изменении.

**Администрирование**

- `/admin24` — статистика плюс полные отчёты **пользователей** и **конвертаций**
  в HTML (по дате, каждая строка — кликабельная ссылка на пользователя).
- `/control` — живая инлайн-панель управления: лимиты размера загрузки, лимиты
  скорости по аудиториям, лимит скорости VLESS-канала, общий лимит полосы и
  «соло-режим» — без передеплоя.
- `/restart` — мягкий перезапуск (перечитывает конфиг и куки).

### Как это работает

```
            Telegram  ──────────────►  Бот (aiogram)  ──►  PostgreSQL (статистика + кэш file_id)
                                          │
                          ┌───────────────┼─────────────────┐
                          ▼               ▼                 ▼
                     yt-dlp          gallery-dl          spotdl
                  (большинство     (фото-посты и      (Spotify → подбор
                   видео/аудио)      карусели)           аудио)
                          │
                          ▼
                 Адаптивный роутер ──► напрямую  (когда платформа доступна)
                                   └─► VLESS-прокси (xray)  (когда заблокирована)
```

- **aiogram 3** — сторона Telegram.
- **yt-dlp** — большая часть извлечения видео/аудио.
- **gallery-dl** — фото/карусели, которые yt-dlp не умеет.
- **spotdl** — метаданные Spotify и подбор аудио.
- **PostgreSQL** — статистика и кэш `file_id` по качеству.
- **xray-core** — VLESS-прокси; опциональный стек **tun2socks + свой Bot API**
  уводит трафик самого Telegram в туннель для загрузок до 2 ГБ.

Всё оркестрируется одним `docker compose` со включаемыми профилями (`vless`,
`local-api`) — запускаете только то, что нужно.

### Решённые сложные проблемы (подводные камни)

Неочевидные грабли, на которые ушла реальная работа — полезно знать и для
деплоя, и если строите похожее.

1. **DPI блокирует не только DNS.** В ограниченных сетях HTTPS платформы может
   работать, а её API/CDN — троттлиться, или наоборот. Единый глобальный прокси
   расточителен и медленен. Бот пробит каждую платформу отдельно и проксирует
   **только** реально заблокированное, плюс повторяет загрузку через прокси при
   *контентной* блокировке (HTTP 403 / гео / rate-limit) уже в процессе.

2. **MTProto Telegram часто троттлится DPI даже там, где HTTPS работает.**
   Поэтому свой Bot API сервер не может достучаться до Telegram напрямую. Решение
   — увести трафик Bot API сервера в VLESS-туннель на сетевом уровне через
   **tun2socks** (обычного SOCKS мало, т.к. Bot API сервер не умеет SOCKS).

3. **Анти-бот челленджи Cloudflare.** Некоторые сайты отдают анти-бот страницу
   IP дата-центров и не-браузерным TLS-отпечаткам. Бот использует **TLS с
   имперсонацией браузера** (curl_cffi) и при необходимости уводит запрос через
   прокси-ноду — с ретраями и ротацией отпечатков, т.к. челлендж прилетает с
   перебоями.

4. **«Застывший кадр» на телефонах.** Телефоны декодируют VP9/AV1 в Telegram
   программно и показывают стоп-кадр со звуком. Сортировка форматов в пользу
   **H.264** чинит инлайн-воспроизведение на всех клиентах.

5. **Лимиты фото в Telegram.** Фото должно быть < 10 МБ *и* сумма сторон ≤ 10000
   px при соотношении не круче 20:1, иначе API возвращает
   `PHOTO_INVALID_DIMENSIONS`. Превышающие лимит картинки отправляются
   документом.

6. **Куки перезаписываются.** yt-dlp сохраняет cookie-jar обратно в файл при
   выходе, поэтому файл должен быть доступен на запись пользователю контейнера —
   иначе каждая загрузка с куками падает (см. раздел решения проблем).

7. **В непривилегированном LXC нет `/dev/net/tun`.** Запуск туннеля внутри
   Proxmox-контейнера требует явного проброса TUN-устройства с хоста (см. гайд по
   Proxmox).

8. **DRM и мёртвые экстракторы.** Spotify не качается напрямую (DRM) и
   подбирается из открытого источника аудио; некоторые сервисы меняют свой
   внутренний API и ломают экстрактор — такие резолвятся иначе.

### Требования

- Linux-хост (VPS, отдельная машина или Proxmox LXC) с **Docker** и плагином
  **Docker Compose**.
- ~2 ГБ ОЗУ комфортно (4 ГБ при включённом стеке 2 ГБ Bot API + туннель).
- **Токен бота Telegram** и **ваш числовой Telegram ID** (для админа).
- *Опционально:* **VLESS-подписка или конфиг(и)**, если сеть блокирует платформы.
- *Опционально:* **Telegram API ID/Hash** для загрузок до 2 ГБ.
- *Опционально:* **файл куки** для платформ, требующих авторизации.

### Быстрый старт

```bash
git clone <ваш-репо> media-bot
cd media-bot
cp .env.example .env
nano .env                 # как минимум BOT_TOKEN и ADMIN_ID

# минимальный запуск (облачный Bot API, 50 МБ, без прокси):
docker compose up -d --build

# логи:
docker compose logs -f bot
```

Включение опциональных стеков — через `COMPOSE_PROFILES` в `.env`:

```ini
# только прокси:
COMPOSE_PROFILES=vless
# прокси + загрузки 2 ГБ:
COMPOSE_PROFILES=vless,local-api
```

### Справочник по настройкам

Вся конфигурация в `.env` (скопируйте из `.env.example`).

| Переменная | Обяз. | Что это |
|---|---|---|
| `BOT_TOKEN` | ✅ | Токен бота от BotFather. |
| `ADMIN_ID` | ✅ | Ваш числовой Telegram ID (админ-команды). |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | ✅ | Креды PostgreSQL (для сервиса `postgres`). |
| `DB_HOST` / `DB_PORT` | — | По умолчанию `postgres` / `5432`. |
| `COMPOSE_PROFILES` | — | Доп. сервисы: `vless`, `local-api`. |
| `MAX_UPLOAD_MB` | — | Жёсткий потолок размера загрузки (по умолч. 2000). |
| `COOKIES_FILE` | — | Путь к файлу куки (по умолч. `data/cookies.txt`). |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | для 2 ГБ | С my.telegram.org; нужны своему Bot API. |
| `TELEGRAM_API_URL` | для 2 ГБ | `http://telegram-bot-api:8081`. |
| `PROXY_URL` | для прокси | `socks5h://xray:2080` — включает адаптивную маршрутизацию. |
| `VLESS_SUBSCRIPTION` | для прокси | URL подписки, прокси сам обновляет. |
| `VLESS_CONFIGS` | для прокси | Или вставьте `vless://...` напрямую через запятую. |
| `VLESS_CONFIGS_FILE` | — | Или укажите файл со строками `vless://...`. |
| `VLESS_UPDATE_INTERVAL` | — | Интервал обновления подписки, сек (по умолч. 21600). |
| `SPOTDL_HTTP_PROXY` | — | HTTP-прокси для метаданных Spotify при гео-блоке (`http://xray:2081`). |
| `MAIN_MAX_MBIT` / `VPN_MAX_MBIT` | — | Верхние границы слайдеров скорости в админ-панели. |

### Как получить каждый кред самому

**Токен бота (`BOT_TOKEN`)**
1. Откройте [@BotFather](https://t.me/BotFather).
2. `/newbot`, задайте имя и username.
3. Скопируйте токен (`123456789:AA...`).

**Ваш ID (`ADMIN_ID`)**
1. Откройте [@userinfobot](https://t.me/userinfobot) (или любой «what is my id»).
2. В ответе — ваш числовой ID, это и есть `ADMIN_ID`.

**Telegram API ID/Hash (для 2 ГБ)**
1. <https://my.telegram.org> → **API development tools**.
2. Создайте приложение; скопируйте **api_id** и **api_hash** в
   `TELEGRAM_API_ID` / `TELEGRAM_API_HASH`.

**VLESS-подписка / конфиги**
- Используйте свою VLESS-ноду. Либо URL подписки в `VLESS_SUBSCRIPTION`, либо
  один-несколько `vless://...` в `VLESS_CONFIGS` (через запятую), либо файл в
  `VLESS_CONFIGS_FILE`. Поддерживаются Reality, xHTTP, WS, gRPC и HTTP, плюс
  балансировка по нескольким нодам с проверкой здоровья.

### Экспорт куки из браузера

Некоторые платформы отдают полный контент только авторизованной сессии. Бот
читает один `data/cookies.txt` (Netscape-формат), где могут быть куки сразу
нескольких сайтов. Залогиньтесь на сайте в браузере и экспортируйте:

```bash
# Экспорт куки одного сайта прямо из профиля браузера через yt-dlp:
yt-dlp --cookies-from-browser firefox --cookies site.txt --skip-download "https://www.tiktok.com/"

# Оставить строки только этого сайта и дописать в общий файл куки:
grep -iE 'tiktok' site.txt >> data/cookies.txt
```

Повторите для каждого сайта (`instagram`, `.x.com|twitter`, `yandex`, …). Затем
сделайте файл **доступным на запись пользователю контейнера** (yt-dlp
переписывает его при выходе):

```bash
chmod 666 data/cookies.txt
```

> Относитесь к `data/cookies.txt` как к паролю — он даёт доступ к вашим
> авторизованным сессиям. По умолчанию он в `.gitignore`; так и оставьте.

### Установка — обычный VPS

Для обычного VPS (Ubuntu/Debian). Если на VPS есть DPI — выполните и шаги с
прокси.

```bash
# 1. Docker + плагин compose
curl -fsSL https://get.docker.com | sh

# 2. Проект и конфиг
git clone <ваш-репо> media-bot && cd media-bot
cp .env.example .env && nano .env       # BOT_TOKEN, ADMIN_ID, DB_* (+ VLESS_* при нужде)

# 3. (опц.) куки — см. раздел про куки; положите data/cookies.txt, затем: chmod 666 data/cookies.txt

# 4. Запуск
docker compose up -d --build
docker compose logs -f bot              # дождитесь "Run polling"
```

Если платформы блокируются — добавьте в `.env`:

```ini
COMPOSE_PROFILES=vless
PROXY_URL=socks5h://xray:2080
VLESS_SUBSCRIPTION=https://ваша-нода/sub/xxxx
```

и снова `docker compose up -d`. Для 2 ГБ добавьте `local-api` в
`COMPOSE_PROFILES` и заполните `TELEGRAM_API_ID/HASH`.

### Установка — Proxmox LXC

Запуск в **непривилегированном** Proxmox LXC полностью поддерживается, с двумя
условиями со стороны хоста. На этой конфигурации проект и закалялся.

**1. Создайте контейнер** (шаблон Debian 12, напр. 1–2 ядра, 4 ГБ ОЗУ для стека
2 ГБ).

**2. Разрешите Docker в LXC** — проще всего включить nesting и keyctl:

```bash
# на хосте Proxmox:
pct set <CTID> --features nesting=1,keyctl=1
```

**3. Пробросьте TUN-устройство** (нужно только для прокси/туннеля — в
непривилегированном LXC нет `/dev/net/tun`). Допишите в
`/etc/pve/lxc/<CTID>.conf` на **хосте**:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

Затем **перезагрузите контейнер с хоста** (обычного restart недостаточно для
привязки устройства):

```bash
pct reboot <CTID>
```

**4. Внутри LXC** ставьте Docker и деплойте как в гайде для VPS:

```bash
curl -fsSL https://get.docker.com | sh
git clone <ваш-репо> media-bot && cd media-bot
cp .env.example .env && nano .env
docker compose up -d --build
```

> Если ваш домашний/Proxmox-аплинк тоже под DPI, именно стек прокси (`vless`) +
> туннель (`local-api`) заставляет работать и скачивание, и загрузки в Telegram —
> ради этого проект и уводит MTProto через tun2socks.

### VLESS-маршрутизация

Включается `COMPOSE_PROFILES=vless` и `PROXY_URL=socks5h://xray:2080`.

- Сервис `xray` собирает конфиг из `VLESS_SUBSCRIPTION` / `VLESS_CONFIGS` /
  `VLESS_CONFIGS_FILE`, поднимает SOCKS на `:2080` и HTTP на `:2081` и
  **балансирует по всем нодам** с observatory-проверкой здоровья.
- **Автообновление** подписки каждые `VLESS_UPDATE_INTERVAL` секунд, перезагрузка
  только при реальном изменении конфига.
- Сам бот делает **адаптивную** маршрутизацию: напрямую пока платформа работает,
  через прокси когда заблокирована, плюс контентный ретрай-через-прокси на
  403/гео/rate-limit. Короткий список платформ можно закрепить на «всегда через
  прокси» в коде для сайтов, которые из вашего региона никогда не идут напрямую.

Сменить ноду — поменяйте `VLESS_*` в `.env` и перезапустите `xray`.

**Конфиг маршрутизации (`data/routing.toml`).** Какой платформе какой маршрут —
задаётся в `routing.toml` (формат — см. `routing.toml.example`), **подхватывается
на лету без рестарта**. Политики: `direct` / `main` (основная нода) / `goida`
(бесплатный пул) / `adaptive` / `vless://...` (выделенная нода для платформы).
`main_fallback = "goida"` — если основная нода умерла, всё уходит в бесплатный
пул, чтобы один сбой не ронял бота.

**Бесплатный резервный пул.** Через `GOIDA_SUBSCRIPTIONS` (raw-ссылки на
агрегаторы вроде AvenCores/goida-vpn-configs) поднимается второй пул на
`socks :2079`: из подписок берутся VLESS-конфиги, xray непрерывно health-чекает
их (`leastLoad` + `burstObservatory`) и крутит запросы по **живым** нодам, дохлые
сами выпадают — без кэша. Используется для IP-забаненных сайтов и как fallback.

### Загрузки до 2 ГБ (свой Bot API)

**Облачный** Bot API ограничивает загрузку 50 МБ. Свой Bot API сервер поднимает
лимит до **2 ГБ**.

```ini
COMPOSE_PROFILES=vless,local-api
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_API_URL=http://telegram-bot-api:8081
PROXY_URL=socks5h://xray:2080
```

Профиль `local-api` поднимает Bot API сервер **и** контейнер `tun2socks`; трафик
Bot API сервера уходит в VLESS-туннель, чтобы достучаться до Telegram даже при
DPI-троттлинге MTProto. Требует включённого профиля `vless`.

### Ограничение полосы

Хостовый systemd-юнит + таймер читают целевую скорость из
`data/bandwidth_mbit.txt` и применяют `tc`-шейпер к интерфейсу контейнера
(egress через `tbf`, ingress через `ifb`), так что **весь** контейнер —
скачивания, загрузки, туннель — не превышает эту скорость. `0` снимает лимит.
Админ меняет значение прямо из панели `/control` (бот пишет файл, таймер
применяет за ~20 с). Скрипты — в `scripts/`.

### Админ-панель и команды

Доступны только `ADMIN_ID`; для остальных их не существует (фильтр на уровне
роутера делает их невидимыми).

- **`/admin24`** — сводка + два HTML-отчёта (пользователи и конвертации), полные
  и по дате, каждый пользователь — кликабельная ссылка.
- **`/control`** — инлайн-панель для живого и постоянного изменения:
  - лимит размера загрузки (юзеры / админ),
  - лимит скорости (юзеры / админ),
  - лимит скорости VLESS-канала (юзеры / админ),
  - общий лимит полосы,
  - **соло-режим** — временно поставить на паузу скачивания всех остальных
    (например, пока вы заливаете что-то тяжёлое) и освободить весь канал себе.
  У каждой настройки есть пресеты и ✏️ ввод своего значения в безопасном
  диапазоне.
- **`/restart`** — мягкий перезапуск; перечитывает `.env` и `data/cookies.txt`.

### Структура проекта

```
bot/                aiogram-бот: хендлеры, загрузчик, БД, прокси-роутер, runtime-конфиг
proxy/              сборщик конфига xray + entrypoint (VLESS-подписка → конфиг)
scripts/            шейпер полосы (systemd service + timer)
compose.yml         весь стек с опциональными профилями
Dockerfile*         образы бота, xray-прокси и своего Bot API
.env.example        скопируйте в .env и заполните
```

### Решение проблем

- **`Run polling` не появляется / getMe висит.** В DPI-сети (локальный) Bot API
  не достучится до Telegram напрямую — включите профили `vless` + `local-api`,
  чтобы трафик шёл через туннель.
- **Любая загрузка с куками падает «Unexpected error».** Файл куки недоступен на
  запись пользователю контейнера (yt-dlp переписывает его при выходе):
  `chmod 666 data/cookies.txt`.
- **Платформа отдаёт 403 / «недоступно в вашей стране».** Включите профиль
  `vless` и задайте `PROXY_URL` — бот уведёт эту платформу через прокси.
- **`PHOTO_INVALID_DIMENSIONS`.** Обрабатывается автоматически — превышающие
  лимит картинки уходят документом. Если видите ошибку — обновите код.
- **Proxmox: туннель падает без `/dev/net/tun`.** Допишите две `lxc.*`-строки в
  конфиг контейнера на хосте и `pct reboot` (см. гайд по Proxmox).
- **Сборка падает на `info.txt`.** Это опциональный legacy-импорт; Dockerfile
  копирует его только при наличии, так что чистый клон собирается без проблем.

---

## 🇬🇧 English version

> This is an infrastructure/automation project. You bring your own bot token,
> your own server and your own accounts; nothing here ships with credentials.

### Contents

- [Features](#features)
- [How it works](#how-it-works)
- [The hard problems it solves](#the-hard-problems-it-solves)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration reference](#configuration-reference)
- [Getting each credential yourself](#getting-each-credential-yourself)
- [Exporting cookies from your browser](#exporting-cookies-from-your-browser)
- [Install — plain VPS](#install--plain-vps)
- [Install — Proxmox LXC](#install--proxmox-lxc)
- [VLESS proxy routing](#vless-proxy-routing)
- [2 GB uploads (self-hosted Bot API)](#2-gb-uploads-self-hosted-bot-api)
- [Bandwidth limiting](#bandwidth-limiting)
- [Admin panel & commands](#admin-panel--commands)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)

### Features

**Downloading**

- **Many platforms** — YouTube (incl. Shorts and YouTube Music), TikTok,
  Instagram, Twitter/X, SoundCloud, Spotify, Yandex Music and more. Just send a
  link; the platform is detected automatically.
- **Maximum quality, no re-encoding.** Video is always taken at the highest
  resolution/FPS and only *remuxed* (never re-encoded) — no quality loss, no
  wasted CPU. Among equal-quality formats it prefers **H.264/AAC**, because mobile
  Telegram plays other codecs (VP9/AV1) as a frozen frame with sound.
- **Quality menu with sizes.** For platforms exposing multiple resolutions, an
  inline menu (e.g. *1080p · 320 MB*) lets the user pick before downloading.
- **Audio extraction.** Every video offers a *"Download audio"* button; YouTube
  Music / SoundCloud / Spotify / Yandex Music links are delivered straight as
  audio. Spotify/Yandex tracks are matched to their audio source automatically
  (Spotify streams are DRM-protected and can't be pulled directly).
- **Photo & carousel posts.** Image posts and carousels (incl. mixed photo+video
  and the attached music track) are sent as a media group. Over-sized or extreme
  aspect-ratio images are sent as documents so Telegram never rejects them.
- **Post captions.** Posts with a caption get a *"Description"* button.
- **Per-quality file cache.** Every uploaded file's Telegram `file_id` is stored
  in PostgreSQL, keyed per resolution; a previously downloaded link is **re-sent
  instantly**, surviving restarts, rebuilds and reboots.

**Reliability & networking**

- **Adaptive proxy routing.** Direct while a platform is reachable, transparent
  VLESS fallback the moment it's blocked/geo-restricted/IP-banned — decided per
  platform and rechecked continuously. Some platforms can be pinned to "always
  via proxy".
- **2 GB uploads.** Optional self-hosted Telegram Bot API server lifts the 50 MB
  cloud limit to 2 GB.
- **Bandwidth cap.** A host-level shaper limits the container's total throughput
  both ways.
- **Self-healing.** All services run under Docker; the proxy auto-refreshes its
  node list and reloads on change.

**Administration**

- `/admin24` — stats plus full **users** and **conversions** HTML reports (sorted
  by date, every row a clickable link to the user).
- `/control` — a live inline panel to change upload size limits, per-audience
  speed limits, the VLESS-channel speed limit, the total bandwidth cap and a
  "solo mode" — without redeploying.
- `/restart` — graceful restart (re-reads config and cookies).

### How it works

```
            Telegram  ──────────────►  Bot (aiogram)  ──►  PostgreSQL (stats + file_id cache)
                                          │
                          ┌───────────────┼─────────────────┐
                          ▼               ▼                 ▼
                     yt-dlp          gallery-dl          spotdl
                  (most video/      (photo posts &     (Spotify → audio
                   audio)            carousels)          match)
                          │
                          ▼
                 Adaptive router ──► direct  (when the platform is reachable)
                                 └─► VLESS proxy (xray)  (when it is blocked)
```

- **aiogram 3** drives the Telegram side; **yt-dlp** handles most video/audio;
  **gallery-dl** handles photo/carousel posts; **spotdl** resolves Spotify.
- **PostgreSQL** stores stats and the per-quality `file_id` cache.
- **xray-core** provides the VLESS proxy; an optional **tun2socks + self-hosted
  Bot API** stack routes Telegram's own traffic through the tunnel for 2 GB
  uploads.

Everything is one `docker compose` stack with optional profiles (`vless`,
`local-api`).

### The hard problems it solves

1. **DPI blocks more than DNS.** A platform's HTTPS may work while its API/CDN is
   throttled. The bot probes each platform independently and proxies **only**
   what's actually blocked, plus retries via proxy on content-level 403/geo/
   rate-limit errors mid-download.
2. **Telegram's MTProto is often DPI-throttled even where HTTPS works**, so a
   self-hosted Bot API can't reach Telegram directly — its traffic is routed
   through the VLESS tunnel at the network layer with **tun2socks** (plain SOCKS
   isn't enough; the Bot API server doesn't speak SOCKS).
3. **Cloudflare anti-bot challenges.** Browser-impersonating TLS (curl_cffi) plus
   proxy routing, retried with rotating fingerprints (the challenge is served
   intermittently).
4. **Mobile "frozen frame" videos.** Preferring **H.264** fixes inline playback.
5. **Telegram photo limits.** Photos must be < 10 MB and sides sum to ≤ 10000 px
   with ratio ≤ 20:1 (`PHOTO_INVALID_DIMENSIONS`); over-limit images go as
   documents.
6. **Cookies get rewritten.** yt-dlp saves the cookie jar back on exit, so the
   file must be writable by the container user.
7. **Unprivileged LXC has no `/dev/net/tun`** — the TUN device must be passed
   through from the Proxmox host.
8. **DRM and dead extractors.** Spotify is matched to a public audio source;
   broken extractors are resolved a different way.

### Requirements

- A Linux host (VPS, dedicated box, or Proxmox LXC) with **Docker** + the
  **Compose plugin**. ~2 GB RAM (4 GB for the 2 GB Bot API + tunnel stack).
- A **bot token** and your **numeric Telegram user ID**.
- *Optional:* a **VLESS subscription/configs**, **Telegram API ID/Hash** (2 GB),
  a **cookies file**.

### Quick start

```bash
git clone <your-repo-url> media-bot
cd media-bot
cp .env.example .env
nano .env                 # at least BOT_TOKEN and ADMIN_ID
docker compose up -d --build
docker compose logs -f bot
```

Enable optional stacks via `COMPOSE_PROFILES` in `.env`:

```ini
COMPOSE_PROFILES=vless            # proxy only
COMPOSE_PROFILES=vless,local-api  # proxy + 2 GB uploads
```

### Configuration reference

| Variable | Required | What it is |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from BotFather. |
| `ADMIN_ID` | ✅ | Your numeric Telegram user ID. |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` | ✅ | PostgreSQL credentials. |
| `DB_HOST` / `DB_PORT` | — | Default `postgres` / `5432`. |
| `COMPOSE_PROFILES` | — | Extra services: `vless`, `local-api`. |
| `MAX_UPLOAD_MB` | — | Hard upload-size ceiling (default 2000). |
| `COOKIES_FILE` | — | Cookies file path (default `data/cookies.txt`). |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | for 2 GB | From my.telegram.org. |
| `TELEGRAM_API_URL` | for 2 GB | `http://telegram-bot-api:8081`. |
| `PROXY_URL` | for proxy | `socks5h://xray:2080`. |
| `VLESS_SUBSCRIPTION` | for proxy | Subscription URL, auto-refreshed. |
| `VLESS_CONFIGS` | for proxy | Or paste `vless://...`, comma-separated. |
| `VLESS_CONFIGS_FILE` | — | Or a file of `vless://...` lines. |
| `VLESS_UPDATE_INTERVAL` | — | Refresh interval, seconds (default 21600). |
| `SPOTDL_HTTP_PROXY` | — | HTTP proxy for Spotify metadata (`http://xray:2081`). |
| `MAIN_MAX_MBIT` / `VPN_MAX_MBIT` | — | Upper bounds for control-panel speed sliders. |

### Getting each credential yourself

- **`BOT_TOKEN`** — [@BotFather](https://t.me/BotFather) → `/newbot`.
- **`ADMIN_ID`** — [@userinfobot](https://t.me/userinfobot) gives your numeric id.
- **`TELEGRAM_API_ID/HASH`** — <https://my.telegram.org> → API development tools.
- **VLESS** — your own node; subscription URL or `vless://` configs (Reality,
  xHTTP, WS, gRPC, HTTP; load-balanced with health checks).

### Exporting cookies from your browser

```bash
yt-dlp --cookies-from-browser firefox --cookies site.txt --skip-download "https://www.tiktok.com/"
grep -iE 'tiktok' site.txt >> data/cookies.txt
chmod 666 data/cookies.txt    # yt-dlp rewrites it on exit
```

Repeat per site (`instagram`, `.x.com|twitter`, `yandex`, …). Treat
`data/cookies.txt` as a password; keep it git-ignored.

### Install — plain VPS

```bash
curl -fsSL https://get.docker.com | sh
git clone <your-repo-url> media-bot && cd media-bot
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f bot       # wait for "Run polling"
```

If platforms are blocked, set `COMPOSE_PROFILES=vless`, `PROXY_URL` and
`VLESS_SUBSCRIPTION`, then `docker compose up -d` again.

### Install — Proxmox LXC

Unprivileged LXC is supported with two host-side steps:

```bash
# on the Proxmox host:
pct set <CTID> --features nesting=1,keyctl=1
```

For the proxy/tunnel stack, pass the TUN device through — append to
`/etc/pve/lxc/<CTID>.conf` on the host:

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

then `pct reboot <CTID>` (a plain restart isn't enough). Inside the LXC, deploy
exactly like the VPS guide.

### VLESS proxy routing

Enable with `COMPOSE_PROFILES=vless` + `PROXY_URL=socks5h://xray:2080`. The
`xray` service builds its config from the subscription/configs, exposes SOCKS
`:2080` and HTTP `:2081`, load-balances across nodes, and auto-refreshes. The bot
routes adaptively (direct → proxy on block) with a content-level retry-via-proxy.

**Routing config (`data/routing.toml`, see `routing.toml.example`)** sets a
per-platform policy — `direct` / `main` / `goida` / `adaptive` / `vless://...` —
and is hot-reloaded (no restart). `main_fallback = "goida"` routes everything
through the free pool if the main node dies, so one failure never takes the bot
down. **Free fallback pool:** set `GOIDA_SUBSCRIPTIONS` to public aggregator URLs
and a second pool comes up on `socks :2079`, continuously health-checked
(`leastLoad` + `burstObservatory`) so requests rotate over live nodes only — used
for IP-banned sites and as the main-node fallback.

### 2 GB uploads (self-hosted Bot API)

```ini
COMPOSE_PROFILES=vless,local-api
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_API_URL=http://telegram-bot-api:8081
PROXY_URL=socks5h://xray:2080
```

`local-api` starts the Bot API server **and** `tun2socks`, routing the server's
traffic through the VLESS tunnel. Needs the `vless` profile too.

### Bandwidth limiting

A host-side systemd unit + timer reads `data/bandwidth_mbit.txt` and applies a
`tc` shaper to the container's interface (egress `tbf`, ingress `ifb`); `0`
removes the cap. Changeable live from `/control`. Scripts in `scripts/`.

### Admin panel & commands

- **`/admin24`** — stats + full users/conversions HTML reports (by date, clickable).
- **`/control`** — live inline panel: upload limits, speed limits (users/admin),
  VLESS-channel speed, total bandwidth cap, and **solo mode** (pause everyone
  else and free the whole pipe). Presets + ✏️ custom value within safe ranges.
- **`/restart`** — graceful restart; re-reads `.env` and cookies.

### Project layout

```
bot/                aiogram bot: handlers, downloader, db, proxy router, runtime config
proxy/              xray config builder + entrypoint
scripts/            bandwidth shaper (systemd service + timer)
compose.yml         the whole stack with optional profiles
Dockerfile*         images for the bot, xray proxy, self-hosted Bot API
.env.example        copy to .env and fill in
```

### Troubleshooting

- **`Run polling` never appears / getMe hangs** — enable `vless` + `local-api`
  so Bot API traffic goes through the tunnel.
- **Cookie downloads fail with "Unexpected error"** — `chmod 666 data/cookies.txt`.
- **403 / "blocked in your country"** — enable `vless` and set `PROXY_URL`.
- **`PHOTO_INVALID_DIMENSIONS`** — handled (over-limit images go as documents).
- **Proxmox: no `/dev/net/tun`** — add the two `lxc.*` lines and `pct reboot`.
- **Build fails on `info.txt`** — optional legacy import; copied only if present.

---

## License

Provided as-is for self-hosting. You are responsible for complying with the terms
of service of any platform you download from and with your local laws. /
Предоставляется как есть для самостоятельного размещения. Вы сами отвечаете за
соблюдение правил используемых платформ и местного законодательства.
