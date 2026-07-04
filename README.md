# Telegram-бот для скачивания видео

Присылаешь боту ссылку на видео из Instagram или YouTube — он возвращает видеофайл. Без подписей, кнопок и лишнего текста.

## Установка

1. Установи Python 3.10+ и ffmpeg.
   - Windows: скачай ffmpeg с https://ffmpeg.org и добавь в PATH.
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`

2. Установи зависимости:
   ```
   pip install -r requirements.txt
   ```

3. Получи токен бота: напиши [@BotFather](https://t.me/BotFather) в Telegram → `/newbot` → скопируй токен.

## Запуск

Задай токен и запусти:

**Windows (PowerShell):**
```
$env:BOT_TOKEN="сюда_токен"
python bot.py
```

**macOS / Linux:**
```
export BOT_TOKEN="сюда_токен"
python bot.py
```

Дальше просто пиши боту ссылку.

## Развёртывание на Render (бесплатно, БЕЗ карты, 24/7)

Render не требует банковскую карту — вход через GitHub. Основной сценарий — Instagram; YouTube опционально (с облачного IP может требовать cookies).

**Шаг 1. Залей файлы на GitHub**
1. Заведи аккаунт на https://github.com (бесплатно, без карты).
2. Нажми **New repository** → имя любое → **Create**.
3. На странице репозитория: **Add file → Upload files** → перетащи все файлы из этой папки (`bot.py`, `requirements.txt`, `Dockerfile`, `render.yaml`) → **Commit changes**.

**Шаг 2. Разверни на Render**
1. Зайди на https://render.com → **Get Started** → войди через **GitHub**.
2. **New → Web Service** → выбери свой репозиторий.
3. Render сам увидит `render.yaml` и `Dockerfile`. План — **Free**.
4. В разделе **Environment** добавь переменную: `BOT_TOKEN` = твой токен от @BotFather.
5. **Create Web Service**. Через пару минут в логах появится «Бот запущен».

**Шаг 3. Не дай боту «уснуть» (важно!)**
Бесплатный Render засыпает после 15 минут без запросов. Чтобы бот отвечал круглосуточно, настрой бесплатный «будильник»:
1. Скопируй URL сервиса из панели Render (вида `https://video-dl-bot.onrender.com`).
2. Зарегистрируйся на https://uptimerobot.com (бесплатно, без карты).
3. **Add New Monitor** → тип **HTTP(s)** → вставь URL → интервал **5 минут** → сохрани.

Теперь бот работает 24/7. Логи — в панели Render.

### Instagram и cookies
Публичные посты/reels обычно качаются без входа. Если Instagram начнёт требовать авторизацию:
1. Расширением «Get cookies.txt LOCALLY» экспортируй cookies с `instagram.com` в файл `cookies.txt`.
2. Загрузи `cookies.txt` в тот же GitHub-репозиторий.
3. В Render добавь переменную `COOKIES_FILE` = `/app/cookies.txt` и передеплой (**Manual Deploy → Deploy latest commit**).

Тот же приём включает YouTube (экспортируй cookies и с `youtube.com`).

---

## Альтернатива: Fly.io (бесплатно, 24/7)

Fly.io держит один маленький сервер включённым постоянно и не усыпляет его. Нужна карта для верификации (в рамках бесплатного лимита списаний нет).

1. Установи Fly CLI:
   - Windows (PowerShell): `iwr https://fly.io/install.ps1 -useb | iex`
   - macOS / Linux: `curl -L https://fly.io/install.sh | sh`

2. Зарегистрируйся и войди:
   ```
   fly auth signup     # или fly auth login, если аккаунт уже есть
   ```

3. В папке с ботом создай приложение (файлы `Dockerfile` и `fly.toml` уже готовы):
   ```
   fly launch --no-deploy
   ```
   На вопросы отвечай: имя — любое свободное, регион — ближе к тебе (например `fra` — Франкфурт, `waw` — Варшава). Существующий `fly.toml` не перезаписывай.

4. Задай токен бота как секрет (он не попадёт в код):
   ```
   fly secrets set BOT_TOKEN="сюда_токен"
   ```

5. Задеплой:
   ```
   fly deploy
   ```

Готово — бот работает круглосуточно. Логи смотри командой `fly logs`.

### Если YouTube просит «подтвердить, что вы не робот»

Это блокировка облачных IP. Обходится через cookies:

1. В браузере, где ты залогинен в YouTube, установи расширение для экспорта cookies в формате Netscape (например «Get cookies.txt LOCALLY»).
2. Экспортируй cookies для `youtube.com` в файл `cookies.txt` и положи его рядом с `bot.py`.
3. В `fly.toml` раскомментируй строку `COOKIES_FILE = "/app/cookies.txt"`.
4. Снова `fly deploy`.

Для Instagram работает так же (экспортируй cookies с `instagram.com` в тот же файл — можно добавить оба сайта).

Как альтернатива блокировкам — прокси: `fly secrets set PROXY="socks5://user:pass@host:port"`.

## Ограничения

- Telegram не даёт боту отправлять файлы больше **50 МБ**. Более крупные видео бот скачать сможет, но не отправит — в этом случае он предупредит.
- Instagram иногда требует авторизации для приватных/некоторых постов. Для таких случаев в `yt_dlp` можно добавить cookies (параметр `cookiefile`).
- Качай только тот контент, на который у тебя есть права. Скачивание чужих видео может нарушать условия использования площадок и авторские права.
