# Normal map: направление для расчёта освещения

Авторский учебный источник по документации Unity 6.0 (6000.0). Общий механизм не является особенностью одного pipeline; пример интерфейса ниже относится к URP/Lit.

Normal map хранит закодированные направления нормалей. Шейдер использует их в расчёте освещения, поэтому царапины и мелкие неровности могут казаться объёмными. Обычный normal mapping сам по себе не перемещает вершины, не добавляет полигоны и не меняет геометрический силуэт. Для изменения формы нужны геометрия или отдельный механизм displacement; это другая операция.

В примере Unity URP назначается совместимый Universal Render Pipeline/Lit, карта подключается в поле Normal Map. Standard Shader — вариант Built-in, а не название Lit в URP. Настройки импорта и соглашение о направлениях должны соответствовать карте и инструментам; произвольный зелёный канал не нужно инвертировать автоматически.

Источники: Unity Manual, Introduction to normal maps (bump mapping), Unity 6.0; Lit shader, Unity 6.0/URP.
https://docs.unity3d.com/6000.0/Documentation/Manual/StandardShaderMaterialParameterNormalMap.html
https://docs.unity3d.com/6000.0/Documentation/Manual/urp/lit-shader.html
