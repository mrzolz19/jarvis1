# Личный голосовой ассистент J.A.R.V.I.S на Python

## Telegram-бот

Проект представляет собой персонального голосового ассистента в Telegram.
Бот принимает текстовые и голосовые сообщения, отправляет запросы в n8n workflow,
получает ответ от ИИ-агентов и возвращает пользователю текстовый и голосовой
ответ.

Бот работает в приватном режиме: сообщения обрабатываются только от владельца,
чей Telegram user ID указан в переменной `OWNER_ID`.

### Архитектура Telegram-бота

- `AppConfig` — читает и проверяет настройки из `.env`.
- `SessionStorage` — управляет диалоговыми сессиями и сохраняет их в
  `user_sessions.json`.
- `N8nClient` — отправляет сообщения в webhook n8n и обрабатывает сетевые
  ошибки, таймауты и SSL fallback.
- `VoiceService` — распознает голос через Groq Whisper и генерирует голосовые
  ответы через локальный Silero TTS.
- `TelegramAssistantBot` — связывает Telegram, n8n, сессии, STT и TTS в единый
  сценарий работы.

Общий сценарий обработки голосового сообщения:

1. Telegram-бот получает голосовое сообщение `.ogg`.
2. `VoiceService` конвертирует аудио в `.wav` через FFmpeg.
3. Groq Whisper `whisper-large-v3` распознает речь и возвращает текст.
4. `N8nClient` отправляет текст и `sessionId` в webhook n8n.
5. n8n workflow выполняет агентную логику, работу с памятью, инструментами или
   RAG и возвращает поле `output`.
6. Бот отправляет текстовый ответ пользователю.
7. `VoiceService` озвучивает ответ через `speakerpy` / Silero TTS и отправляет
   голосовое сообщение.

Текстовые сообщения проходят тот же путь, но без этапа распознавания речи.

### Основные библиотеки

- `pyTelegramBotAPI` (`telebot`) — работа с Telegram Bot API.
- `groq` — транскрипция аудио через Groq Whisper и генерация коротких названий
  сессий.
- `speakerpy` и `silero` — локальный синтез речи на базе PyTorch.
- `ffmpeg-python` — конвертация аудио между OGG, WAV и MP3.
- `requests` — HTTP-запросы к webhook n8n с повторами при таймаутах.

### 📁 Структура проекта
```plaintext
├── main.py                   # Основной исполняемый файл бота
├── requirements.txt          # Зависимости Python-окружения
├── .env                      # Файл с конфигурацией и API-ключами (создается вручную)
├── latest_silero_models.yml  # Справочник доступных моделей и ссылок Silero
└── user_sessions.json        # Файл истории сессий пользователей (генерируется автоматически)
```

## ⚙️ Инструкция по установке и запуску

### 1. Телеграм-бот

#### Установка

1. Установите [n8n](https://docs.n8n.io/choose-n8n/) локально или на сервер.

2. Установите FFmpeg. Он нужен для конвертации голосовых сообщений и
   сгенерированных аудиофайлов.

	- Linux (Ubuntu/Debian):

	```bash
 	sudo apt update && sudo apt install ffmpeg -y
 	```
 
 	- macOS (через Homebrew):

	```bash
 	brew install ffmpeg
 	```
 
	- Windows: Скачайте бинарные файлы с официального сайта FFmpeg, распакуйте и добавьте путь к папке bin в системную переменную PATH

3. Клонируйте репозиторий и перейдите в директорию проекта:

	```bash
	git clone https://github.com/mrzolz19/jarvis1.git
	cd jarvis1
	```

4. Создайте виртуальное окружение и установите зависимости:

	```bash
	python -m venv venv

	# Активация для Linux/macOS:
	source venv/bin/activate

	# Активация для Windows:
	venv\Scripts\activate

	pip install -r requirements.txt
	```

5. Создайте или отредактируйте файл `.env` в корне проекта.

6. Скачайте модель TTS:
   `https://models.silero.ai/models/tts/ru/v5_cis_ext.pt`

   Поместите модель в папку, которую использует локальная библиотека
   `speakerpy` в вашем окружении проекта.

#### Настройка

##### n8n

1. Запустите n8n.
2. Импортируйте workflow `JARVIS Personal Voice Assistant v4.json`.
3. Скопируйте webhook из Chat Trigger или Webhook-ноды.
4. Укажите ссылку в `.env` в переменной `WEBHOOK_N8N`.

##### Телеграмм

1. Создайте бота через `@BotFather`.
2. Скопируйте Telegram Bot API token в `.env`:

	```env
	BOT_TELEGRAM_API=ваш_telegram_bot_token
	```

3. Узнайте свой Telegram user ID, например через `@userinfobot`, и укажите его:

	```env
	OWNER_ID=ваш_telegram_user_id
	```

4. Создайте Groq API key в [Groq Console](https://console.groq.com/keys) и
   добавьте его в `.env`:

	```env
	GROQ_API_KEY=ваш_groq_api_key
	```

5. Добавьте webhook n8n:

	```env
	WEBHOOK_N8N=http://localhost:5678/webhook/your-webhook
	```

6. При необходимости настройте таймауты и повторы запросов к n8n:

	```env
	N8N_CONNECT_TIMEOUT=10
	N8N_READ_TIMEOUT=300
	N8N_RETRY_ATTEMPTS=2
	N8N_RETRY_DELAY_SECONDS=1.5
	```

### 2. Установка локально:
1. Установите [n8n](https://docs.n8n.io/choose-n8n/) локально (через Docker или NodeJS) или на сервер
2. Клонируйте и подготовте окружение
- Склонируйте репозиторий и перейдите в его директорию:

	```bash
	git clone https://github.com/mrzolz19/jarvis1.git
	cd telegram_bot
	```
- Создайте виртуальное окружение и установите зависимости:
	```bash
	python -m venv venv
	# Активация для Linux/macOS:
	source venv/bin/activate
	# Активация для Windows:
	venv\Scripts\activate

	pip install -r requirements.txt
	```

#### Настройка:
##### n8n
1. Активируйте n8n, установите мой workflow и скопируйте из Chat Trigger свой Webhook и в файле .env вставьте ссылку WEBHOOK_N8N после равно.

##### Настройка конфигурационного файла:
- Перейдите в settings.ini чтобы вставить скопированный Chat Webhook в строчку после "=" webhook_n8n
- Установите при необходимости нужные команды в Commands для выхода из программы

## 🚀 Использование и запуск

### Telegram бот

После запуска `main.py` бот начинает слушать входящие сообщения через polling.
Можно отправлять:

- обычные текстовые сообщения;
- голосовые сообщения Telegram.

В обоих случаях бот отправляет запрос в n8n workflow, получает ответ и
возвращает:

- текстовое сообщение;
- голосовую озвучку ответа.

Если голосовое сообщение не содержит разборчивой речи, бот попросит повторить
команду четче.

### Сессии диалога

Каждый диалог связан с `sessionId`. Этот идентификатор отправляется в n8n вместе
с запросом, чтобы workflow мог хранить и восстанавливать контекст разговора.

Сессии сохраняются в файле `user_sessions.json`. Файл создается автоматически.

Доступные команды управления контекстом:

- `/help` — показать краткую справку по командам.
- `/new` или `/newsession` — создать новую сессию диалога.
- `/session` — показать текущую активную сессию.
- `/sessions` — вывести список последних 10 сессий.
- `/switch <session_id>` — переключиться на существующую сессию.
- `/rmsession <session_id>` — удалить сессию.

### Запуск

Запускать голосового ассистента нужно из корневого каталога проекта:

```bash
python main.py
```
```bash
python main_pyttsx3.py
```
```bash
python main_silerotts.py

После запуска в консоли появятся логи загрузки сессий и работы бота. Для
остановки нажмите `Ctrl+C`. Для пропуска озвучки в настольной версии нажмите клавишу "q"
