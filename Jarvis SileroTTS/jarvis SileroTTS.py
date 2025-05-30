import speech_recognition as sr
import random
import sys
from configparser import ConfigParser
from os import environ
environ['PYGAME_HIDE_SUPPORT_PROMPT'] = '1' #убираем вывод от pygame
from pygame import mixer
from speakerpy.lib_speak import Speaker as sp
import requests
import uuid
import openwakeword
from openwakeword.model import Model
import pyaudio
import numpy as np

class MicrophoneManager:
    def __init__(self):
        self.mic = sr.Microphone()
        self.is_active = False
        self.audio_stream = None

    def __enter__(self):
        if not self.is_active:
            self.audio_stream = self.mic.__enter__()
            self.is_active = True
        return self.audio_stream

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.is_active:
            self.mic.__exit__(exc_type, exc_val, exc_tb)
            self.is_active = False
            self.audio_stream = None

def safe_microphone_control(enable: bool): #управление микрофоном (вкл, выкл)
    if enable and not mic_manager.is_active:
        mic_manager.__enter__()
    elif not enable and mic_manager.is_active:
        mic_manager.__exit__(None, None, None)

def play_text(text): #озвучивние текста
    safe_microphone_control(False)
    speaker.speak(text=str(text), sample_rate=48000, speed=1.10, put_accent=True, put_yo=True)

def hi(): #функция приветствия после активационное фразы
    mixer.music.load(f"sound/greet{random.choice([1, 2, 3])}.wav")
    mixer.music.play()
    while mixer.music.get_busy():
        safe_microphone_control(False)
    print("К вашим услугам, сэр")

def brain(text): #запрос в n8n и получение ответа от него
    data = {
        "chatInput": text,
        "sessionId": session_id
    }

    response = requests.post(webhook_n8n, json=data)
    resault_response = response.json()
    print(resault_response['output'])
    return resault_response['output']


def handle_commands():
    try:
        while True:
            with mic_manager:
                try:
                    print("Слушаю...")
                    recognizer.adjust_for_ambient_noise(source=mic_manager.mic, duration=0.65)
                    audio = recognizer.listen(mic_manager.mic, timeout=timeout)
                    text = recognizer.recognize_google(audio, language="ru")
                    text_for_cmd = text.lower().strip().replace('!', '').replace('.', '').replace('?', '').replace(',', '')
                    print(f"Вы сказали: {text}")
                    handled = False


                    #Команды:
                    if text_for_cmd in cmd_exit:
                        print("Отключаю питание")
                        mixer.music.load("sound/off_power.wav")
                        mixer.music.play()
                        sys.exit()
                        handled = True

                    if not text.strip():
                        print("Пустая команда")
                        continue

                #Анализ сказанного:
                except sr.WaitTimeoutError:
                    print("Вы где? Жду команды...")
                    return
                except sr.UnknownValueError:
                    print("Речь не распознана")
                    continue

                if not handled:
                    play_text(brain(text))
                    break

    except sr.RequestError as e:
        print(f"Ошибка сервиса: {e}")
    except Exception as e:
        print(f"Ошибка: {e}")

def main():
    mixer.init()
    mixer.music.load("sound/run.wav")
    mixer.music.play()
    try:
        oww_model = Model(
            wakeword_models=[model_path],
            inference_framework='onnx'
        )
    except Exception as e:
        print(f"Ошибка загрузки модели: {e}")
        return

    # Параметры аудиопотока
    sample_rate = 16000  # частота дискретизации, поддерживаемая моделью
    chunk_size = 1280

    # Инициализация PyAudio
    audio_interface = pyaudio.PyAudio()
    stream = audio_interface.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=sample_rate,
        input=True,
        frames_per_buffer=chunk_size
    )

    print("Ожидаю активационную фразу...")
    while True:
        # Чтение аудиоданных напрямую из потока
        audio_data = np.frombuffer(stream.read(chunk_size), dtype=np.int16)

        #wake word
        prediction = oww_model.predict(audio_data)
        if prediction['hey jarvis'] > 0.25:
            stream.stop_stream()
            oww_model.reset()
            hi()
            handle_commands()
            stream.start_stream()
            print("Ожидаю активационную фразу...")

if __name__ == "__main__":
    mic_manager = MicrophoneManager()
    model_path = "hey jarvis"
    config = ConfigParser()
    with open("settings.ini", "r", encoding="utf-8") as f:
        config.read_file(f)

    cmd_exit = config["Commands"]["Cmd_Exit"]
    #Speech Recognition
    timeout = int(config["Speech"]["TimeoutSpeechRecognition"]) #через сколько секунд снова обращаться к wake word после отсуствия звуков
    speaker = sp(model_id="v3_1_ru", language="ru", speaker="aidar", device="cuda")

    # Распознователь речи speech_recognition
    recognizer = sr.Recognizer()
    recognizer.pause_threshold = 1 #фраза будет завершённой после этого таймаута в сек

    if not(config['Settings']['webhook_n8n'] and config['Settings']['OpenWakeWord_download_models']):
        session_id = str(uuid.uuid4())
    #Сохраняем в ini:
        webhook_n8n = input("Введите webhook n8n: ")
        openwakeword.utils.download_models(["hey jarvis"])
        config['Settings']['webhook_n8n'] = webhook_n8n
        config['Settings']['OpenWakeWord_download_models'] = "downloaded"

        # Записываем изменения в файл
        with open('settings.ini', 'w', encoding='utf-8') as configfile:
            config.write(configfile)
        main()

    else:
        webhook_n8n =config['Settings']['webhook_n8n']
        session_id = str(uuid.uuid4())
        main()
