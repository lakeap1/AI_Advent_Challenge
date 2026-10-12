# Unity 6.0: Linear, Gamma и sRGB

Авторский краткий пересказ официальной документации Unity Technologies, проверено 2026-10-11. Версия: Unity 6.0 (6000.0). Источник: https://docs.unity3d.com/6000.0/Documentation/Manual/set-project-color-space.html .

## Настройка проекта

Unity поддерживает Linear и Gamma workflow. Настройка Color Space находится в Edit → Project Settings → Player → Other Settings → Rendering.

Текстуры часто сохранены в gamma color space, а вычисления шейдеров ожидают linear color space. sRGB sampler переводит значения gamma-текстуры в линейные при выборке, чтобы вычисления выполнялись в согласованном пространстве. Настройка проекта и настройка конкретной текстуры решают разные части этого перехода.

Вопрос об импорте карты числовых данных требует дополнительно проверить её Texture Type и sRGB sampling. Правило отключения sRGB описано в отдельном документе корпуса.
