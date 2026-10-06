# EMANAGER Dialer: копайлот для оператора

Оверлей поверх всех окон Windows показывает оператору Zadarma короткие подсказки
во время звонка. Стек: PyQt6-клиент, стерео-аудио по WebSocket, NVIDIA Parakeet
на бэкенде, поиск по базе знаний в Supabase pgvector и Gemini с мастер-промптом
EMANAGER.PRO.

```
[ПК оператора]                               [Бэкенд (GPU)]                          [Облако]
 микрофон ─► L ┐                               VAD по каналам (Silero)
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
| `backend/app/stt.py` | VAD-нарезка по каналам + Parakeet с батчингом запросов всех операторов на одной GPU |
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

### 2. Бэкенд (Linux + NVIDIA GPU)
```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cu124   # под вашу CUDA
pip install -r requirements.txt "nemo_toolkit[asr]>=2.4"
cp .env.example .env        # GEMINI_API_KEY, DATABASE_URL, AUTH_TOKEN
python -m tools.ingest_kb   # загрузить kb/ в Supabase
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Проверка без софтфона и без GPU: `STT_ENGINE=none` в `.env`, затем
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
- **Parakeet и польский язык.** `parakeet-tdt-0.6b-v2` понимает только английский, поэтому взята
  `nvidia/parakeet-tdt-0.6b-v3` (25 европейских языков, включая польский). Модель не потоковая:
  реплика распознаётся целиком, как только VAD видит 450 мс тишины. Итоговый бюджет примерно такой:
  0,45 с паузы + 0,1–0,2 с STT + ~0,03 с RAG + 0,8–1,5 с Gemini.
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

Проверено в облачной Linux-среде: 19 юнит-тестов (бэкенд и клиент), сквозной прогон
клиент ↔ WebSocket ↔ бэкенд с подставной LLM, схема SQL и загрузка/поиск/логирование на Postgres 16 +
pgvector, сборка запроса к Gemini (google-genai 2.28), отрисовка всех экранов оверлея (картинка выше).

Не проверено, нужен ваш Windows-ПК и GPU-сервер: реальный захват WASAPI (PyAudioWPatch есть только
под Windows), распознавание Parakeet, загрузка модели e5 (Hugging Face закрыт в моей среде),
живые вызовы Gemini и фактическая задержка, расход памяти клиентом на Windows (на Linux демо-режим
занимает ~53 МБ).
