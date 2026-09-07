# AI Advent Challenge

Репозиторий практических заданий по созданию приложений с AI. Структура рассчитана на 7 недель по 5 заданий; каждое решение находится в собственной папке и запускается независимо.

## Что уже работает

[Задание 1.1 — запрос к LLM через API](week_1/task_1/README.md): локальное веб-приложение на Python и Flask. Пользователь вводит вопрос в браузере, приложение отправляет его модели Luna и показывает ответ. В репозитории есть исходный код, тесты и инструкция запуска.

[Задание 1.2 — управление форматом ответа](week_1/task_2/README.md): один чат с GPT-4.1 mini и переключателями JSON, лимита слов и настоящей stop sequence через Chat Completions API.

[Задание 1.3 — разные способы рассуждения](week_1/task_3/README.md): задача о мосте, четыре способа через API, шесть проверенных итоговых маршрутов и сравнение.

[Задание 1.4 — температура](week_1/task_4/README.md): один запрос, три ответа Luna при 0, 0.7 и 1.2, реальные примеры и выводы по применению.

[Задание 1.5 — версии моделей](week_1/task_5/README.md): Luna, Terra и Sol, одинаковый запрос, три прогона, сравнение времени, токенов, стоимости и качества.

Статус и материалы каждого задания указаны в его README.

## Навигация

| Неделя | Задания |
|---|---|
| 1 | [1.1](week_1/task_1/README.md) · [1.2](week_1/task_2/README.md) · [1.3](week_1/task_3/README.md) · [1.4](week_1/task_4/README.md) · [1.5](week_1/task_5/README.md) |
| 2 | [2.1](week_2/task_1/README.md) · [2.2](week_2/task_2/README.md) · [2.3](week_2/task_3/README.md) · [2.4](week_2/task_4/README.md) · [2.5](week_2/task_5/README.md) |
| 3 | [3.1](week_3/task_1/README.md) · [3.2](week_3/task_2/README.md) · [3.3](week_3/task_3/README.md) · [3.4](week_3/task_4/README.md) · [3.5](week_3/task_5/README.md) |
| 4 | [4.1](week_4/task_1/README.md) · [4.2](week_4/task_2/README.md) · [4.3](week_4/task_3/README.md) · [4.4](week_4/task_4/README.md) · [4.5](week_4/task_5/README.md) |
| 5 | [5.1](week_5/task_1/README.md) · [5.2](week_5/task_2/README.md) · [5.3](week_5/task_3/README.md) · [5.4](week_5/task_4/README.md) · [5.5](week_5/task_5/README.md) |
| 6 | [6.1](week_6/task_1/README.md) · [6.2](week_6/task_2/README.md) · [6.3](week_6/task_3/README.md) · [6.4](week_6/task_4/README.md) · [6.5](week_6/task_5/README.md) |
| 7 | [7.1](week_7/task_1/README.md) · [7.2](week_7/task_2/README.md) · [7.3](week_7/task_3/README.md) · [7.4](week_7/task_4/README.md) · [7.5](week_7/task_5/README.md) |

## Как запустить

Откройте README нужного задания и выполняйте команды из его папки. Общего запуска для всего репозитория нет: задания могут использовать разный стек.

Для приложений первой недели нужны Python и API-ключ OpenAI с доступом к выбранной модели. Команды установки и запуска приведены в README каждого задания. Ключ задаётся локально через `.env`; в репозитории используется только шаблон `.env.example` без секретов.
