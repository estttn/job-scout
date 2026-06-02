# FitLetter

Подбор вакансий на HeadHunter под ваш профиль: fit-скоринг, персональные сопроводительные письма, трекер откликов.

## Ветки

| Ветка | Назначение |
|-------|------------|
| **dev** | Разработка. Вся работа и выгрузки сюда. |
| **main** | Продакшен. Merge из `dev` по запросу ? автодеплой на сервер. |

```text
feature work ? dev ? (по запросу) merge to main ? GitHub Actions ? VPS
```

## Локальный запуск

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # заполните DEEPSEEK_API_KEY
cp profile.example.json profile.json
uvicorn app.main:app --host 0.0.0.0 --port 8090 --reload
```

## Деплой (main)

При push в `main` срабатывает `.github/workflows/deploy-main.yml`.

**Secrets в GitHub** (Settings ? Secrets ? Actions):

| Secret | Пример |
|--------|--------|
| `DEPLOY_HOST` | `89.108.98.245` |
| `DEPLOY_USER` | `root` |
| `DEPLOY_SSH_KEY` | приватный ключ SSH (deploy) |

На сервере сохраняются: `data/`, `.env`, `profile.json` — в git не попадают.

## Сбор вакансий

Кнопка «Обновить с HH» сначала загружает и сохраняет вакансии (без ожидания писем), затем в фоне параллельно генерирует сопроводительные. Параллелизм настраивается переменными окружения:

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `COLLECT_DESC_WORKERS` | `8` | Потоки загрузки описаний с HH |
| `COLLECT_LETTER_WORKERS` | `15` | Потоки запросов к DeepSeek |

## Структура

```text
app/           FastAPI, collector, scorer, letters
app/templates/ UI
scripts/       утилиты (purge, regen)
data/          SQLite (не в git)
```
