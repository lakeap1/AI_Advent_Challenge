# Unity 6.0 / URP: SRP Batcher

Авторский краткий пересказ официальной документации Unity Technologies, проверено 2026-10-11. Версия: Unity 6.0 (6000.0), URP. Источник: https://docs.unity3d.com/6000.0/Documentation/Manual/SRPBatcher.html .

## Что ускоряется

SRP Batcher уменьшает CPU-время подготовки и отправки draw calls для материалов, использующих один shader variant. Он уменьшает смены состояния рендеринга между draw calls; это отличается от уменьшения самого количества draw calls.

SRP batch содержит последовательность bind и draw команд. Для эффективности желательно меньше shader variants; разных материалов с тем же shader может быть много. Данные материалов сохраняются в constant buffers памяти GPU.

## Совместимость

SRP Batcher поддерживается в URP, HDRP и custom Scriptable Render Pipeline. Built-in Render Pipeline его не поддерживает. Поэтому нельзя переносить вывод о включении SRP Batcher из URP на Built-in.
