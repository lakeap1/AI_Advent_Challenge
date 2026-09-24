'use strict';

// Layout state is local; agent data and actions remain owned by app.js.
(() => {
  const shell = document.querySelector('.app-shell');
  const dialog = document.getElementById('workspace-dialog');
  const title = document.getElementById('workspace-panel-title');
  const notice = document.getElementById('panel-notice');
  const titles = { profile: 'Профиль и персонализация', task: 'План и состояние задачи',
    rules: 'Правила задачи', sources: 'Источники знаний', usage: 'Расходы и состав запросов',
    branches: 'Ветки разговора', create: 'Новый диалог или задача' };
  let returnFocus = null;
  let openPanelName = null;

  window.openWorkspacePanel = (name, trigger = null) => {
    if (!Object.hasOwn(titles, name)) return;
    openPanelName = name;
    if (!dialog.open) returnFocus = trigger || document.activeElement;
    title.textContent = titles[name];
    document.querySelectorAll('[data-workspace-panel]').forEach(panel => {
      panel.hidden = panel.dataset.workspacePanel !== name;
    });
    document.querySelectorAll('[data-panel]').forEach(button => {
      button.setAttribute('aria-expanded', String(button.dataset.panel === name));
      button.setAttribute('aria-controls', 'workspace-dialog');
    });
    notice.textContent = '';
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
    dialog.querySelector('[data-close-panel]').focus();
  };
  dialog.addEventListener('close', () => {
    document.querySelectorAll('[data-panel]').forEach(button => button.setAttribute('aria-expanded', 'false'));
    const fallback = document.querySelector('.hud [data-panel="' + openPanelName + '"]')
      || document.querySelector('.profile-launch');
    (returnFocus?.isConnected && !returnFocus.disabled ? returnFocus : fallback).focus();
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || button.disabled) return;
    if (button.dataset.panel) {
      window.openWorkspacePanel(button.dataset.panel, button);
      if (button.hasAttribute('data-profile-memory')) dialog.querySelector('.remembered-section').scrollIntoView({ block: 'start' });
    }
    if (button.hasAttribute('data-close-panel')) dialog.close();
    if (button.dataset.shellToggle) {
      const mobile = window.matchMedia('(max-width:900px)').matches;
      const chats = button.dataset.shellToggle === 'chats';
      const name = chats ? 'chats-mobile-open' : mobile ? 'context-mobile-open' : 'context-hidden';
      const enabled = shell.classList.toggle(name);
      if (enabled && mobile) shell.classList.remove(chats ? 'context-mobile-open' : 'chats-mobile-open');
      const expanded = chats || mobile ? enabled : !enabled;
      document.querySelectorAll('[data-shell-toggle="' + button.dataset.shellToggle + '"]').forEach(control => control.setAttribute('aria-expanded', String(expanded)));
    }
    if (button.classList.contains('dialogue-item')) {
      shell.classList.remove('chats-mobile-open');
      document.querySelector('[data-shell-toggle="chats"]').setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('workspace-notice', event => {
    if (!dialog.open) return;
    const data = event.detail || {};
    notice.textContent = data.message || data.text || '';
    notice.className = 'notice ' + (data.kind || data.type || '');
    if (notice.textContent) notice.scrollIntoView({ block: 'nearest' });
  });
  const setText = (id, value) => { document.getElementById(id).textContent = value; };
  const stages = { planning: 'Планирование', execution: 'Выполнение', validation: 'Проверка', done: 'Завершено' };
  document.addEventListener('workspace-state', event => {
    const state = event.detail;
    const active = state.workspace?.active_dialogue;
    const task = state.workspace?.tasks?.find(item => item.id === active?.task_id);
    const progress = state.task_state || {};
    const profile = state.personalization?.profile || {};
    setText('profile-launch-name', profile.name || 'Профиль');
    setText('context-task-name', task?.name || 'Задача не выбрана');
    setText('context-goal', progress.goal || 'Опишите в чате, в чём хотите разобраться.');
    setText('context-stage', progress.paused ? 'На паузе' : stages[progress.stage] || '—');
    setText('context-step', progress.current_step ? 'Сейчас: ' + progress.current_step : 'План появится по ходу разговора.');
    const formats = { plain: 'Обычный текст', steps: 'Нумерованные шаги', markdown: 'Markdown' };
    const preferences = [profile.style || 'Стиль по умолчанию', formats[profile.format] || 'Обычный текст'];
    if (profile.constraints) preferences.push(profile.constraints);
    setText('context-preferences', preferences.join('\n'));
    const rules = state.invariants?.rules || [];
    setText('context-rules', rules.length ? 'Обязательных правил задачи: ' + rules.length : 'Особые правила задачи не заданы.');
    const count = document.getElementById('knowledge-count').textContent;
    setText('context-sources', count !== '0' ? 'Сохранённых результатов поиска: ' + count : 'Поиск ещё не выполнялся.');
    updateSourceLabel();
  });
  function updateSourceLabel() {
    const select = document.getElementById('knowledge-sources');
    setText('source-launch', 'Источники: ' + (select.selectedOptions[0]?.textContent || 'без поиска'));
  }
  document.getElementById('knowledge-sources').addEventListener('change', updateSourceLabel);
  document.getElementById('prompt').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!document.getElementById('send').disabled) document.getElementById('ask-form').requestSubmit();
    }
  });
  const narrow = window.matchMedia('(max-width:900px)');
  function syncLayout() {
    shell.classList.remove('context-mobile-open', 'chats-mobile-open');
    document.querySelectorAll('[data-shell-toggle="context"]').forEach(button => button.setAttribute('aria-expanded', String(!narrow.matches && !shell.classList.contains('context-hidden'))));
    document.querySelectorAll('[data-shell-toggle="chats"]').forEach(button => button.setAttribute('aria-expanded', 'false'));
  }
  narrow.addEventListener('change', syncLayout);
  new ResizeObserver(() => {
    const bottom = document.querySelector('.hud').getBoundingClientRect().bottom;
    shell.style.setProperty('--workspace-header-height', bottom + 'px');
  }).observe(document.querySelector('.hud'));
  syncLayout();
  updateSourceLabel();
})();
