# EMANAGER Dialer: копайлот для оператора

Оверлей поверх всех окон Windows показывает оператору Zadarma короткие подсказки
во время звонка. Стек: PyQt6-клиент, стерео-аудио по WebSocket, NVIDIA Parakeet
на процессоре бэкенда (без видеокарты), поиск по базе знаний в Supabase pgvector и Gemini с мастер-промптом
EMANAGER.PRO.

```
[ПК оператора]                               [Бэкенд (CPU)]                          [Облако]
 микрофон ─► L ┐                               VAD по каналам (Silero ONNX)
               ├─ 16 кГц s16le stereo ─WS─►    Parakeet TDT 0.6B v3 (pl) ─► [Klient]/[Operator]
 loopback ─► R ┘   (100 мс кадры)              RAG: e5-small ─► Supabase pgvector ─► Gemini (JSON)
 PyQt6 оверлей ◄────────── JSON-подсказка ◄────────────────────────────────────┘
```

![Состояния оверлея](docs/overlay_states.png)

## Структура

| Путь | Что внутри |
|---|---|
| `client/` | Десктоп-клиент Windows: оверлей, захват аудио, связь с бэкендом |
| `client/dialer_client/overlay.py`, `widgets.py`, `theme.py` | UI по макету: панель 400×56, виджеты подсказок, ошибок, итога звонка, развёрнутая панель 420×720 |
| `client/dialer_client/audio.py` | Захват: L = микрофон, R = WASAPI loopback (PyAudioWPatch), ресемплинг в 16 кГц (soxr) |
| `client/dialer_client/net.py` | WebSocket с автопереподключением и буфером на 60 с (подсказки «догоняют» разговор) |
| `client/dialer_client/detect.py` | Определение начала и конца звонка по звуку, проверка процесса Zadarma |
| `backend/app/main.py` | FastAPI, WebSocket `/ws`, описание протокола в начале файла |
| `backend/app/stt.py` | VAD-нарезка по каналам (Silero ONNX) + Parakeet через onnx-asr на CPU (int8), батчинг запросов всех операторов |
| `backend/app/langfilter.py` | Отбрасывает английские фразы, которые Parakeet v3 иногда выдаёт на тихом звуке |
| `backend/models/silero_vad.onnx` | Модель Silero VAD (MIT), взята из recorder_fork |
| `backend/app/session.py` | RAG-контроллер: фильтр реплик, поиск, вызов Gemini, дедупликация, обогащение JSON для UI |
| `backend/app/llm.py` | Gemini: мастер-промпт делится на статичную `system_instruction` и динамический блок |
| `backend/app/rag_store.py` | Эмбеддинги e5 (`query:`/`passage:`), поиск через asyncpg, логирование звонков |
| `backend/sql/001_schema.sql` | Схема Supabase: `kb_items` (vector 384, HNSW), `match_kb()`, `calls`, `call_utterances`, `call_hints` |
| `backend/kb/` | База знаний: `*.jsonl` (Q&A, возражения, скрипты) и `regulations/*.md` (регламенты) |
| `backend/tools/` | `ingest_kb` (загрузка базы), `extract_qa` (пары Q&A из удачных звонков), `simulate_call` (тест без софтфона) |
| `prompts/master_prompt_pl.md` | Мастер-промпт EMANAGER.PRO (польский) |

## Запуск

### 1. Supabase
1. SQL Editor → вставить `backend/sql/001_schema.sql` → Run.
2. Строку подключения взять в Project Settings → Database (Session pooler, порт 5432) и положить в `DATABASE_URL`.

### 2. Бэкенд (любой ПК или сервер, Windows или Linux, видеокарта не нужна)
```bash
cd backend
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU-версия для e5, без CUDA
pip install -r requirements.txt
cp .env.example .env        # GEMINI_API_KEY, DATABASE_URL, AUTH_TOKEN, PARAKEET_THREADS
python -m tools.ingest_kb   # загрузить kb/ в Supabase
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
При первом запуске onnx-asr скачает модель Parakeet int8 (~670 МБ) с Hugging Face. Для офлайн-сервера
положите файлы модели в папку и укажите её в `PARAKEET_MODEL_PATH`.

Проверка без софтфона и без распознавания: `STT_ENGINE=none` в `.env`, затем
`python -m tools.simulate_call --script tools/demo_dialogue.txt --token <AUTH_TOKEN>`.
Для проверки Parakeet: `--wav call.wav` (стерео 16 кГц, L = оператор, R = клиент).

### 3. Клиент (Windows 10/11, Python 3.11+)
```bat
cd client
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy config.example.toml config.toml   :: server_url, token
python run.py --demo    :: только UI, без звука и сервера
python run.py
```
Если Zadarma выводит звук не в устройство по умолчанию (например, в гарнитуру), выберите его через
логотип на панели → «Dźwięk rozmówcy» или укажите `line_device` в `config.toml`.

## Решения, которые стоит знать

- **Модель Gemini.** Gemini 1.5 Flash отключена. По умолчанию стоит `gemini-3.8-flash`
  (стабильная, сентябрь 2026) с `thinking_level=low`. Если задержка выше 2,5 с, переключите
  `GEMINI_MODEL=gemini-3.5-flash-lite`. Обе меняются в `.env` без правки кода.
- **Parakeet на процессоре, как в recorder_fork.** Parakeet TDT 0.6B v3 (25 европейских языков,
  включая польский) запускается через `onnx-asr` на onnxruntime, только CPU, квантизация int8.
  Silero VAD тоже работает из ONNX-файла, поэтому бэкенду не нужны ни видеокарта, ни NeMo.
  Перед распознаванием звук проходит фильтр 80 Гц и нормализацию, а после — фильтр английских
  фраз (v3 нельзя жёстко переключить на польский). Модель не потоковая: реплика распознаётся
  целиком, как только VAD видит 450 мс тишины. Время распознавания растёт с длиной реплики;
  `PARAKEET_THREADS` (2–4) задаёт, сколько ядер отдать модели.
- **Роли без диаризации.** Спикер определяется каналом: L всегда оператор, R всегда клиент.
- **Захват.** WASAPI loopback ничего не отдаёт, пока в динамиках тишина, а у двух звуковых карт
  разные часы. Микшер работает по 100-мс тактам от системных часов, дополняет пустой канал нулями и
  отбрасывает отставание больше 300 мс, так что каналы не разъезжаются.
- **Сжатие.** Сейчас по сети идёт несжатый PCM 16 кГц стерео (~64 КБ/с на оператора). Для локальной
  сети и VPN этого достаточно. Opus можно добавить позже, протокол это позволяет.
- **Начало и конец звонка.** Локального API у Zadarma нет, поэтому звонок определяется по голосу в канале
  клиента (≥0,6 с) и заканчивается после 15 с тишины. Последние 3 с до старта досылаются, первые слова
  не теряются. Есть ручной старт и стоп в меню. Точные границы дадут вебхуки АТС Zadarma
  (`NOTIFY_START`/`NOTIFY_END`), их можно подключить к бэкенду следующим шагом.
- **Приватность.** Вне звонка и на паузе аудио с ПК не уходит.
- **Мастер-промпт.** Статичная часть (до `---`) уходит в `system_instruction`, блок с
  `{rag_context}`, `{last_hint}`, `{transcript_history}` уходит в `contents`. Для модели это тот же
  текст, но одинаковый префикс включает неявное кэширование Gemini. Подстановка идёт через
  `str.replace`, потому что в промпте есть фигурные скобки JSON.
- **Обогащение подсказки.** Gemini возвращает только `show/category/hint`. Бэкенд сам добавляет
  цитату клиента, «dopasowanie NN%» (сходство лучшего совпадения), чипы источников и
  «Inny wariant» (готовые `hint` из равнозначных совпадений базы).
- **Защита от повторов.** Одновременно идёт один запрос к Gemini на звонок. Новые реплики во время
  запроса схлопываются до последней, а одинаковая подсказка второй раз не показывается.

## Что проверено, а что нет

Проверено в облачной Linux-среде: 25 юнит-тестов, включая настоящий Silero VAD на onnxruntime (бэкенд и клиент), сквозной прогон
клиент ↔ WebSocket ↔ бэкенд с подставной LLM, схема SQL и загрузка/поиск/логирование на Postgres 16 +
pgvector, сборка запроса к Gemini (google-genai 2.28), отрисовка всех экранов оверлея (картинка выше).

Не проверено, нужен ваш Windows-ПК: реальный захват WASAPI (PyAudioWPatch есть только
под Windows), распознавание Parakeet и его скорость на вашем процессоре, загрузка моделей Parakeet и e5
(Hugging Face закрыт в моей среде),
живые вызовы Gemini и фактическая задержка, расход памяти клиентом на Windows (на Linux демо-режим
занимает ~53 МБ).
