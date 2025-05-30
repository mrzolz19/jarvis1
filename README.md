# Личный голосовой асситент J.A.R.V.I.S на python v.0.1.

## Установка 
1. Установите [n8n](https://docs.n8n.io/choose-n8n/) локально (или используйте n8n Cloud)
2. Перейдите в папку с нужной версией и установите все зависимости:
	`pip install -r requirements.txt`
3. Скопируйте папку в Jarvis SileroTTS speakerpy в папку с расположением пакетов python (Lib\site-packages)

## Настройка:
### n8n
1. Активируйте n8n
	`n8n`

2. Перейдите в Overview и создайте шаблон

![chrome_TuGP7qCR33](https://github.com/user-attachments/assets/58a1c543-b50f-4201-ae0f-e27ea29c8345)
![chrome_qVjPbO9hqE](https://github.com/user-attachments/assets/89a5ac67-704d-4ace-918c-1e4b78e2dae5)
![chrome_Q3q3cAeNPh](https://github.com/user-attachments/assets/51fb4d05-5464-4d02-ab81-1cbafd7e994f)

3. Импортируйте мой шаблон JARVIS.json
4. Подключите [API DeepSeek](https://platform.deepseek.com/api_keys) и другие инструменты
5. Перейдите в настройки Trigger, скопируйте Chat URL

![7rIy3xN3nm](https://github.com/user-attachments/assets/6f987e54-a492-4438-b0c0-bc29daf4c446)
![ShareX_LNCB8DrpCf 1](https://github.com/user-attachments/assets/b6e4b16c-a81f-4b02-b594-dc15a1605190)

6. Активируйте workflow

![szr2asRAaE](https://github.com/user-attachments/assets/929ca5e5-6cad-435f-8e5a-ac3c8b83baec)


### Python:
- Перейдите в settings.ini чтобы вставить скопированный Chat URL в строчку после "=" webhook_n8n
- Установите при необходимости нужные команды в Commands для выхода из программы

## Запустите:
`python jarvis pyttsx3.py`
`python jarvis SileroTTS.py`
