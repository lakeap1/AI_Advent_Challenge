# День 30 — приватный сервис с локальной LLM

Помощник по компьютерной графике и арту работает на собственной CPU VPS. Браузерный чат и HTTP API используют Qwen3.5 4B через Ollama, без облачного переключения и автоматических повторов генерации.

Это продолжение задания 6.2: явно переиспользуется только `week_6/task_2/local_agent`, включая политики, SQLite и учёт расходов. Другие приложения прошлых заданий не нужны для запуска. Стек — Python, Flask, Waitress и Ollama.

```mermaid
flowchart LR
    Browser["Браузер / HTTP-клиент"] -->|"localhost:5030"| SSH["SSH-туннель"]
    SSH -->|"VPS 127.0.0.1:5030"| API["Bearer API + ограничения"]
    API --> Agent["Агент + SQLite"]
    Agent -->|"127.0.0.1:11435"| Ollama["Ollama / Qwen3.5 4B / CPU"]
```

API и Ollama слушают только loopback VPS. Сетевой доступ проходит через SSH; API дополнительно требует Bearer-ключ. Один ключ открывает один общий чат: все обладатели ключа видят одинаковую историю. Многопользовательской изоляции здесь нет.

## Конфигурация и ограничения

Проверенная VPS: Linux x86_64, Intel Xeon Sapphire Rapids, 4 vCPU, 8 ГБ RAM, 120 ГБ диска; вычислительного GPU нет. Используется Ollama 0.40.3 и `qwen3.5:4b`, Q4_K_M. `num_gpu=0` явно задаёт CPU-режим каждого запроса; поле `size_vram` не используется как доказательство наличия GPU.

| Ограничение | Значение |
| --- | --- |
| Контекст модели | 4096 токенов |
| Максимальный ответ | 384 токена, `think=false` |
| Вопрос / HTTP-тело | до 2000 символов / до 32 000 байт |
| Частота | 10 авторизованных попыток `/ask` за 60 секунд |
| Исполнение | один запрос к модели одновременно |
| Очередь | до трёх ожидающих запросов |
| Ожидание очереди / модели | до 180 секунд на каждый этап |

Окно частоты общее для владельца и не зависит от `X-Forwarded-For`. Отказ по частоте возвращает 429 с `Retry-After`; переполнение или тайм-аут очереди — 503. Перезапуск процесса сбрасывает окно частоты и счётчики admission; история и квитанции остаются в SQLite.

Полная история не обрезается автоматически. До inference агент проверяет число символов и консервативный UTF-8 бюджет с резервом для оформления сообщений и ответа. Длинная история может получить 400 раньше, чем достигнуты 4096 фактических токенов. Это бюджетная оценка, а не точный токенизатор.

## Развёртывание

На VPS нужны Python 3.12+, `curl`, `zstd`, `tar`, `systemd --user` и работающий SSH-доступ. Скрипты выполняются обычным пользователем и проверяют конфликтующие порты и отличающиеся unit-файлы. Новый каталог — `~/ai-advent-day30`; старые сервисы и cron не меняются.

Из корня репозитория создайте архив по явному списку производственных файлов. История, ключи, тестовые данные и видео в него не попадут:

```
python -B week_6/task_5/scripts/build-bundle.py --output day30-app.tar.gz
ssh user@VPS "mkdir -p ~/ai-advent-day30/app"
scp day30-app.tar.gz user@VPS:ai-advent-day30/app/
ssh user@VPS
```

Замените `user@VPS` вашим настроенным SSH target. При необходимости передайте `-i` и `-o UserKnownHostsFile=...`. На VPS:

```
tar -xzf ~/ai-advent-day30/app/day30-app.tar.gz -C ~/ai-advent-day30/app
bash -n ~/ai-advent-day30/app/week_6/task_5/deploy/install-runtime.sh
bash -n ~/ai-advent-day30/app/week_6/task_5/deploy/pull-model.sh
bash -n ~/ai-advent-day30/app/week_6/task_5/deploy/deploy-api.sh
bash ~/ai-advent-day30/app/week_6/task_5/deploy/install-runtime.sh
systemctl --user start ai-advent-day30-ollama.service
bash ~/ai-advent-day30/app/week_6/task_5/deploy/pull-model.sh
bash ~/ai-advent-day30/app/week_6/task_5/deploy/deploy-api.sh
systemctl --user start ai-advent-day30.service
systemctl --user enable ai-advent-day30-ollama.service ai-advent-day30.service
systemctl --user is-active ai-advent-day30-ollama.service ai-advent-day30.service
loginctl show-user "$USER" -p Linger
```

Для сохранения user services после выхода из SSH нужен `Linger=yes`. Если он выключен, включение выполняет администратор.

Установка проверяет SHA-256 официального архива Ollama. Скрипт модели сохраняет фактический digest и квантовку в `data/model-manifest.json`; повторный запуск проверяет сохранённый digest. Bearer-ключ создаётся один раз в `~/ai-advent-day30/data/access-token` с правами 0600 и не печатается установщиком. Рабочие данные остаются в каталоге сервиса.

## Подключение

На Windows откройте туннель; его терминал должен оставаться открытым:

```
./week_6/task_5/scripts/open-tunnel.ps1 -Server VPS -User user -IdentityFile C:/path/to/private-key -KnownHostsFile C:/path/to/known_hosts
```

Получите ключ в игнорируемый локальный файл:

```
New-Item -ItemType Directory -Force week_6/task_5/data
scp -i C:/path/to/private-key -o UserKnownHostsFile=C:/path/to/known_hosts user@VPS:ai-advent-day30/data/access-token week_6/task_5/data/access-token
```

Откройте `http://127.0.0.1:5030`, введите ключ в поле подключения и задайте вопрос. Ключ находится только в памяти вкладки; после перезагрузки его нужно ввести снова. Пока другой клиент выполняет запрос, страница ограниченно обновляет состояние без повторной генерации.

## HTTP API

| Метод и путь | Результат |
| --- | --- |
| `GET /api/private/state` | История, квитанции, стоимость, политики и ограничения |
| `GET /api/private/runtime` | Готовность, модель, версия и заданный CPU-режим |
| `POST /api/private/ask` | Один вопрос, ответ, receipt и актуальное состояние |

Пример PowerShell без ключа в строке команды:

```
$key = (Get-Content -Raw week_6/task_5/data/access-token).Trim()
$headers = @{ Authorization = "Bearer $key" }
$body = @{ prompt = "Объясни roughness одним предложением." } | ConvertTo-Json -Compress
$bytes = [Text.Encoding]::UTF8.GetBytes($body)
Invoke-RestMethod -Uri http://127.0.0.1:5030/api/private/ask -Method Post -Headers $headers -ContentType "application/json; charset=utf-8" -Body $bytes
```

## Учёт и политики

Фактические `prompt_eval_count` и `eval_count` Ollama сохраняются как input/output tokens; total вычисляется их суммой с указанием источника. Сообщённый cached input записывается отдельно; отсутствующие reasoning и другие метрики остаются неизвестными.

Токенный API стоит 0 USD, поскольку модель размещена самостоятельно. Модель, тариф, режим, основание расчёта и дата проверки записываются в квитанциях. Расходы на хостинг и электричество зависят от выбранной инфраструктуры; их сумма по умолчанию неизвестна и не включена в токенный тариф.

Input policy проверяет тип, пустой ввод, Unicode, длину и бюджет истории до inference. Output policy проверяет структуру и завершение ответа, наличие текста и его максимальную длину. Семантическая правильность и безопасность содержания этим не проверяются. Требование обычного текста передаётся в системной инструкции; программной очистки Markdown нет.

При неизвестном usage или незавершённой квитанции после сбоя дальнейшие вопросы блокируются, включая после рестарта. Отказ output policy сохраняет расход выполненного вызова и не выдаёт текст как успешный ответ. Скрытых повторов и автоматического сброса блока нет. История сохраняется для работы чата; неуспешные тексты не записываются только ради учёта.

## Проверки

Локальные тесты покрывают авторизацию, rate limit, очередь, отзывчивость state во время генерации, отказ контекста до inference, Unicode, политики и долговечное блокирование. Браузерный сценарий с управляемым API проверяет ключ, 401, XSS, pending/unknown, ошибки и полную историю; модель он не вызывает.

```
python -m pip install -r week_6/task_5/requirements.txt pytest
$env:PYTHONPATH = ".;week_6/task_2"
python -B -m pytest -p no:cacheprovider week_6/task_5/tests week_6/task_2/tests_local/test_local_agent.py week_6/task_2/tests_local/test_day30_compatibility.py
node week_6/task_5/scripts/check-private-browser.cjs
```

Для браузерного сценария нужны Playwright с Chromium и переменная `PLAYWRIGHT_NODE_MODULES`, указывающая на каталог `node_modules`.

Команды выполняются из корня исходников в PowerShell. Для Linux задайте `PYTHONPATH=.:week_6/task_2`. Указанный набор не требует остальных приложений предыдущих дней.

Сетевой probe выполняет шесть настоящих вопросов, затем проверяет 401, 400 и 429. Используйте свежий отдельный экземпляр с `DAY30_API_PORT=5031` и отдельным `DAY30_DATA_DIR`, чтобы тестовая история не заняла бюджет рабочего чата. Туннель должен перенаправлять выбранный порт. Скрипт сохраняет ответы и квитанции и прекращает дальнейшую генерацию при ошибке.

```
python -B week_6/task_5/scripts/probe_service.py --base-url http://127.0.0.1:5031 --token-file week_6/task_5/data/access-token --output probe.json
```

После явного перезапуска только API проверьте сохранность данных без нового вопроса:

```
python -B week_6/task_5/scripts/probe_service.py --base-url http://127.0.0.1:5031 --token-file week_6/task_5/data/access-token --verify-restart probe.json --output restart.json
```

Большие запросы обрабатываются медленнее, а длительность чата ограничена небольшим контекстом. Длительная нагрузка и качество советов модели не оценены этими проверками.

Модель может ошибаться в формулировках и выдавать посторонние символы. Проверки сервиса не оценивают смысл ответа.

## Управление сервисом

```
systemctl --user status ai-advent-day30.service ai-advent-day30-ollama.service
journalctl --user -u ai-advent-day30.service -n 50 --no-pager
systemctl --user stop ai-advent-day30.service ai-advent-day30-ollama.service
```

Остановка этих двух unit-файлов сохраняет модель, историю и квитанции. Полное удаление данных скрипты не выполняют.
