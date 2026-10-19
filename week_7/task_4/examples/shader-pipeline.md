# Материалы в Unity: выбранный pipeline

Авторский учебный источник. Пример: Unity 6.0 (6000.0), Universal Render Pipeline (URP), шейдер Universal Render Pipeline/Lit. Имена свойств других шейдеров могут отличаться.

В Inspector материала выбранный Shader определяет доступные настройки. Lit в URP даёт свойства Base Map, Metallic, Smoothness и Normal Map. Их значения и карты управляют разными частями расчёта поверхности. Base Map задаёт базовый цвет, Normal Map влияет на нормаль для освещения.

Нельзя переносить конкретный shader asset между Built-in, URP и HDRP как заведомо совместимый. Standard Shader относится к Built-in; в этом примере используется URP/Lit.

Для проверки связи понятий можно взять один объект, оставить совместимый шейдер и по очереди изменить цвет материала и назначенную карту. Это предложенное упражнение, а не результат запуска Unity сервисом.

Источники: Unity Manual, Material Inspector window reference и Lit shader, Unity 6.0/URP.
https://docs.unity3d.com/6000.0/Documentation/Manual/class-Material.html
https://docs.unity3d.com/6000.0/Documentation/Manual/urp/lit-shader.html
