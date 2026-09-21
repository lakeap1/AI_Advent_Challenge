'use strict';

const $ = selector => document.querySelector(selector);

const dom = {
  refresh: $('#state-refresh'),
  bootStatus: $('#boot-status'),
  profileSelect: $('#profile-select'),
  currentProfileTitle: $('#current-profile-title'),
  profileSavedStatus: $('#profile-saved-status'),
  profileSummary: $('#profile-summary'),
  profileRefs: $('#profile-refs'),
  profileForm: $('#profile-form'),
  profileStyle: $('#profile-style'),
  profileFormat: $('#profile-format'),
  profileConstraints: $('#profile-constraints'),
  profileCreateForm: $('#profile-create-form'),
  profileName: $('#profile-name'),
  dialogueSelect: $('#dialogue-select'),
  openDialogue: $('#open-dialogue'),
  dialogueCount: $('#dialogue-count'),
  dialogueForm: $('#dialogue-form'),
  dialogueName: $('#dialogue-name'),
  dialogueMode: $('#dialogue-mode'),
  taskForm: $('#task-form'),
  taskName: $('#task-name'),
  taskProject: $('#task-project'),
  taskMode: $('#task-mode'),
  taskStageBadge: $('#task-stage-badge'),
  taskPauseBadge: $('#task-pause-badge'),
  savedTaskGoal: $('#saved-task-goal'),
  savedTaskStage: $('#saved-task-stage'),
  savedTaskPlan: $('#saved-task-plan'),
  taskPlanEmpty: $('#task-plan-empty'),
  taskValidation: $('#task-validation'),
  taskProgress: $('#task-progress'),
  savedTaskStep: $('#saved-task-step'),
  savedTaskAction: $('#saved-task-action'),
  savedTaskNotes: $('#saved-task-notes'),
  toggleTaskPause: $('#toggle-task-pause'),
  taskStateHelp: $('#task-state-help'),
  activeTitle: $('#active-title'),
  activeTask: $('#active-task'),
  activeMode: $('#active-mode'),
  conversationTitle: $('#conversation-title'),
  conversation: $('#conversation'),
  waiting: $('#waiting'),
  notice: $('#notice'),
  agentStatus: $('#agent-status'),
  sessionInfo: $('#session-info'),
  askForm: $('#ask-form'),
  prompt: $('#prompt'),
  promptCount: $('#prompt-count'),
  useWorking: $('#use-working'),
  useLongTerm: $('#use-long-term'),
  preview: $('#preview-button'),
  previewResult: $('#token-preview'),
  memoryCount: $('#memory-count'),
  shortCount: $('#short-count'),
  workingCount: $('#working-count'),
  longCount: $('#long-count'),
  shortList: $('#memory-short'),
  workingList: $('#memory-working'),
  longList: $('#memory-long'),
  memoryEditor: $('#memory-editor'),
  memoryForm: $('#memory-form'),
  memoryEditId: $('#memory-edit-id'),
  memoryLayer: $('#memory-layer'),
  memoryCategory: $('#memory-category'),
  memoryScope: $('#memory-scope'),
  memoryKey: $('#memory-key'),
  memoryValue: $('#memory-value'),
  memoryReason: $('#memory-reason'),
  memoryCancel: $('#memory-cancel'),
  chatCost: $('#chat-cost'),
  chatTokens: $('#chat-tokens'),
  chatCalls: $('#chat-calls'),
  costNote: $('#cost-note'),
  requestCount: $('#request-count'),
  requestList: $('#request-list'),
  tracePanel: $('#trace-panel'),
  traceStatus: $('#trace-status'),
  traceContent: $('#trace-content'),
  branchPanel: $('#branch-panel'),
  checkpointForm: $('#checkpoint-form'),
  checkpointName: $('#checkpoint-name'),
  checkpointSelect: $('#checkpoint-select'),
  branchForm: $('#branch-form'),
  branchName: $('#branch-name'),
  branchSelect: $('#branch-select'),
  switchForm: $('#switch-form'),
  activeBranch: $('#active-branch'),
};

const modeNames = {
  sliding: 'Окно сообщений',
  facts: 'Окно + факты',
  branching: 'Ветвление',
};

const requestKinds = {
  answer: 'Ответ',
  extraction: 'Извлечение памяти',
  facts: 'Обновление facts',
  validation: 'Проверка этапа',
};

const statusNames = {
  ok: 'готово',
  rejected: 'отклонено',
  error: 'ошибка',
  interrupted: 'прервано',
  pending: 'выполняется',
};

const categoryNames = {
  context: 'Контекст задачи',
  profile: 'Профиль пользователя',
  decision: 'Принятое решение',
  knowledge: 'Знание',
};

const scopeNames = {
  task: 'текущая задача',
  user: 'пользователь local',
  project: 'текущий проект',
};

const formatNames = {
  plain: 'Обычный текст',
  steps: 'Нумерованные шаги',
  markdown: 'Markdown',
};

const stageNames = {
  planning: 'Планирование',
  execution: 'Выполнение',
  validation: 'Проверка',
  done: 'Завершено',
};

const profileExamples = {
  beginner: {
    name: 'Демо · Новичок',
    style: 'Объясняй специальные термины простыми словами при первом употреблении.',
    format: 'steps',
    constraints: 'Давай ровно 3 нумерованных шага. Используй Blender и Cycles; не предлагай платные аддоны.',
  },
  experienced: {
    name: 'Демо · Опытный',
    style: 'Отвечай кратко, без объяснения базовых терминов.',
    format: 'markdown',
    constraints: 'Используй Blender и Cycles; не меняй движок и не предлагай платные ассеты.',
  },
};

let currentState = null;
let busy = false;
let ready = false;
let knownMessageIds = new Set();
let activeDialogueIdentity = null;
let renderedProfileId = null;
let profileDirty = false;

function element(tag, className = '', text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null && text !== undefined) node.textContent = String(text);
  return node;
}

function option(value, text) {
  const node = element('option', '', text);
  node.value = String(value);
  return node;
}

function integer(value) {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat('ru-RU').format(number) : '—';
}

function money(value) {
  if (value === null || value === undefined || value === '') return 'неизвестно';
  const number = Number(value);
  if (!Number.isFinite(number)) return 'неизвестно';
  return '$' + number.toFixed(number < 0.01 ? 6 : 4);
}

function dateTime(value) {
  if (!value) return 'время не указано';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('ru-RU');
}

function jsonText(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? '—');
  }
}

function errorText(body, fallback) {
  if (body && typeof body.text === 'string' && body.text.trim()) return body.text;
  if (body && typeof body.error === 'string' && body.error.trim()) return body.error;
  return fallback;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  let body = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  return {response, body};
}

function setNotice(text = '', type = '') {
  dom.notice.textContent = text;
  dom.notice.className = type ? 'notice ' + type : 'notice';
}

function updatePromptCount() {
  const count = Array.from(dom.prompt.value).length;
  dom.promptCount.textContent = integer(count) + ' / 12 000';
  syncControls();
}

function updateTaskStateHelp(taskState) {
  if (taskState?.stage === 'done') {
    dom.taskStateHelp.textContent = 'Задача завершена. Создайте новую задачу или откройте диалог другой задачи, чтобы продолжить работу.';
  } else if (taskState?.paused === true) {
    dom.taskStateHelp.textContent = 'Задача на паузе. Введённый вопрос остаётся в поле; нажмите «Продолжить», чтобы снова открыть чат.';
  } else {
    dom.taskStateHelp.textContent = 'Пишите в чат как обычно: состояние обновится вместе со следующим успешным ответом.';
  }
}

function syncControls() {
  const disableAll = busy;
  document.querySelectorAll('button, input, textarea, select').forEach(control => {
    control.disabled = disableAll;
  });
  if (disableAll) return;

  const selectedProfile = currentState?.personalization?.profile;
  const taskState = currentState?.task_state;
  const paused = taskState?.paused === true;
  const done = taskState?.stage === 'done';
  const taskBlocked = paused || done;
  const active = activeWorkspace();
  const dialogues = Array.isArray(currentState?.workspace?.dialogues) ? currentState.workspace.dialogues : [];
  const selectedDialogue = dialogues.find(item => String(item.id) === String(dom.dialogueSelect.value));
  const opensOtherTask = selectedDialogue && String(selectedDialogue.task_id) !== String(active?.task_id);

  dom.profileSelect.disabled = !ready || !dom.profileSelect.value;
  dom.profileStyle.disabled = !ready || !selectedProfile;
  dom.profileFormat.disabled = !ready || !selectedProfile;
  dom.profileConstraints.disabled = !ready || !selectedProfile;
  $('#save-profile').disabled = !ready || !selectedProfile || !profileDirty;
  $('#create-profile').disabled = !ready || !dom.profileName.value.trim();
  document.querySelectorAll('[data-profile-example]').forEach(button => { button.disabled = !ready; });
  dom.openDialogue.disabled = !ready || !dom.dialogueSelect.value || (done && !opensOtherTask);
  dom.dialogueName.disabled = !ready || done;
  dom.dialogueMode.disabled = !ready || done;
  $('#create-dialogue').disabled = !ready || !dom.dialogueName.value.trim() || done;
  $('#create-task').disabled = !ready || !dom.taskName.value.trim();
  dom.toggleTaskPause.disabled = !ready || !taskState || done;
  dom.toggleTaskPause.textContent = paused ? 'Продолжить' : 'Поставить на паузу';
  updateTaskStateHelp(taskState);
  dom.prompt.disabled = !ready || taskBlocked;
  dom.useWorking.disabled = !ready || taskBlocked;
  dom.useLongTerm.disabled = !ready || taskBlocked;
  $('#send').disabled = !ready || !dom.prompt.value.trim() || taskBlocked;
  dom.preview.disabled = !ready || !dom.prompt.value.trim() || taskBlocked;
  $('#save-memory').disabled = !ready || done || !dom.memoryKey.value.trim() || !dom.memoryValue.value.trim() || !dom.memoryReason.value.trim();
  const editingMemory = Boolean(dom.memoryEditId.value);
  dom.memoryLayer.disabled = editingMemory || done;
  dom.memoryCategory.disabled = editingMemory || done;
  dom.memoryScope.disabled = editingMemory || done;
  dom.memoryKey.disabled = done;
  dom.memoryValue.disabled = done;
  dom.memoryReason.disabled = done;
  dom.memoryCancel.disabled = done;
  dom.memoryKey.readOnly = editingMemory;
  dom.memoryKey.setAttribute('aria-readonly', editingMemory ? 'true' : 'false');
  dom.checkpointName.disabled = !ready || done;
  dom.checkpointSelect.disabled = !ready || done;
  dom.branchName.disabled = !ready || done;
  dom.branchSelect.disabled = !ready || done;
  $('#create-checkpoint').disabled = !ready || done || !dom.checkpointName.value.trim();
  $('#create-branch').disabled = !ready || done || !dom.checkpointSelect.value || !dom.branchName.value.trim();
  $('#switch-branch').disabled = !ready || done || !dom.branchSelect.value;
  document.querySelectorAll('.memory-card .move-select').forEach(select => { select.disabled = !ready || done; });
  document.querySelectorAll('[data-action]:not([data-action="trace"])').forEach(button => { button.disabled = !ready || done; });
}

function setBusy(value, label = '') {
  busy = value;
  dom.waiting.hidden = !value;
  const taskState = currentState?.task_state;
  const readyLabel = taskState?.stage === 'done'
    ? 'Задача завершена'
    : taskState?.paused === true ? 'Задача на паузе' : 'Готов к вопросу';
  dom.agentStatus.textContent = value ? label || 'Выполняется' : ready ? readyLabel : 'Нет соединения';
  dom.agentStatus.classList.toggle('is-busy', value);
  syncControls();
}

function activeWorkspace(state = currentState) {
  return state?.workspace?.active_dialogue ?? null;
}

function selectedProfileId(state = currentState) {
  return state?.personalization?.selected_id ?? state?.personalization?.profile?.id ?? null;
}

function activeRequestIdentity(state = currentState) {
  const taskId = state?.task_state?.task_id ?? activeWorkspace(state)?.task_id;
  const profileId = selectedProfileId(state);
  return {
    task_id: taskId === null || taskId === undefined ? null : Number(taskId),
    profile_id: profileId === null || profileId === undefined ? null : Number(profileId),
  };
}

function taskStateValues(taskState) {
  return {
    goal: typeof taskState?.goal === 'string' ? taskState.goal : '',
    current_step: typeof taskState?.current_step === 'string' ? taskState.current_step : '',
    expected_action: typeof taskState?.expected_action === 'string' ? taskState.expected_action : '',
    notes: typeof taskState?.notes === 'string' ? taskState.notes : '',
  };
}

function profileValues(profile) {
  return {
    style: typeof profile?.style === 'string' ? profile.style : '',
    format: Object.prototype.hasOwnProperty.call(formatNames, profile?.format) ? profile.format : 'plain',
    constraints: typeof profile?.constraints === 'string' ? profile.constraints : '',
  };
}

function profileDraft() {
  return {
    style: dom.profileStyle.value.trim(),
    format: dom.profileFormat.value,
    constraints: dom.profileConstraints.value.trim(),
  };
}

function setProfileDirty(value) {
  profileDirty = Boolean(value);
  dom.profileSavedStatus.textContent = profileDirty ? 'Есть несохранённые изменения' : 'Сохранено';
  dom.profileSavedStatus.classList.toggle('locked', !profileDirty);
  dom.profileSavedStatus.classList.toggle('unsaved', profileDirty);
  syncControls();
}

function profileSummaryPair(term, description) {
  const wrapper = element('div');
  wrapper.append(element('dt', '', term), element('dd', '', description || 'Не задано'));
  return wrapper;
}

function renderPersonalization(state, options = {}) {
  const personalization = state?.personalization ?? {};
  const profiles = Array.isArray(personalization.profiles) ? personalization.profiles : [];
  const profile = personalization.profile && typeof personalization.profile === 'object'
    ? personalization.profile
    : null;
  const selectedId = personalization.selected_id ?? profile?.id ?? null;

  dom.profileSelect.replaceChildren(...profiles.map(item => option(item.id, item.name || 'Профиль #' + item.id)));
  if (profiles.some(item => String(item.id) === String(selectedId))) dom.profileSelect.value = String(selectedId);

  const values = profileValues(profile);
  dom.currentProfileTitle.textContent = profile?.name || 'Профиль не выбран';
  dom.profileSummary.replaceChildren(
    profileSummaryPair('Стиль', values.style),
    profileSummaryPair('Формат', formatNames[values.format]),
    profileSummaryPair('Ограничения', values.constraints),
  );
  const refs = Array.isArray(profile?.refs) ? profile.refs : [];
  dom.profileRefs.textContent = refs.length
    ? 'Версии настроек: ' + refs.map(ref => '#' + String(ref.id ?? '—') + ' · версия ' + String(ref.revision ?? '—')).join('; ')
    : 'Версии настроек: пока не сохранены';

  const profileChanged = String(renderedProfileId) !== String(selectedId);
  if (options.force || profileChanged || !profileDirty) {
    dom.profileStyle.value = values.style;
    dom.profileFormat.value = values.format;
    dom.profileConstraints.value = values.constraints;
    renderedProfileId = selectedId;
    setProfileDirty(false);
  }
}

function confirmProfileDraftLoss(actionText) {
  if (!profileDirty) return true;
  return window.confirm('Есть несохранённый черновик профиля. ' + actionText + ' отбросит его. Продолжить?');
}

function availableProfileName(baseName) {
  const names = new Set((currentState?.personalization?.profiles ?? []).map(item => String(item.name ?? '')));
  if (!names.has(baseName)) return baseName;
  let suffix = 2;
  while (names.has(baseName + ' ' + suffix)) suffix += 1;
  return baseName + ' ' + suffix;
}

function requestKind(request) {
  const kind = request?.metadata?.kind ?? request?.kind ?? 'answer';
  return requestKinds[kind] ?? String(kind);
}

function policyText(policy) {
  if (!policy) return 'не сообщено';
  if (typeof policy === 'string') return policy;
  if (policy.passed === true || policy.status === 'passed' || policy.status === 'ok') return 'пройдена';
  if (policy.passed === false || policy.status === 'rejected') return 'не пройдена';
  return policy.status ? String(policy.status) : 'проверена';
}

function emptyCopy(text) {
  return element('p', 'empty-copy', text);
}

function resetTrace() {
  dom.traceStatus.textContent = 'Выберите вызов';
  dom.traceContent.replaceChildren(emptyCopy('Нажмите «Показать контекст» у вызова ответа, чтобы увидеть версии памяти и сообщения, вошедшие в запрос.'));
}

function resetDialogueUi() {
  resetMemoryEditor();
  dom.memoryEditor.open = false;
  resetTrace();
  dom.previewResult.textContent = '';
  dom.previewResult.hidden = true;
  requestAnimationFrame(() => dom.prompt.focus({preventScroll: true}));
}

function renderTaskState(state) {
  const taskState = state?.task_state;
  if (!taskState || typeof taskState !== 'object') {
    throw new Error('Сервер не вернул состояние текущей задачи.');
  }
  const values = taskStateValues(taskState);
  const stageName = stageNames[taskState.stage] ?? String(taskState.stage ?? '—');
  const paused = taskState.paused === true;
  const done = taskState.stage === 'done';

  dom.savedTaskGoal.textContent = values.goal || 'Не задана';
  dom.savedTaskStage.textContent = stageName;
  const plan = Array.isArray(taskState.plan) ? taskState.plan : [];
  dom.savedTaskPlan.replaceChildren();
  for (const [index, step] of plan.entries()) {
    const item = document.createElement('li');
    const action = document.createElement('p');
    const criterion = document.createElement('p');
    action.textContent = step.action;
    criterion.className = 'task-plan-criterion';
    criterion.textContent = 'Проверка: ' + step.criterion;
    item.append(action, criterion);
    const progress = taskState.step_results?.find(result => result.step === index + 1);
    const status = {pending: 'Ожидает результата', passed: 'Критерий выполнен', failed: 'Критерий не выполнен'};
    item.append(element('p', 'task-step-status', status[progress?.status] ?? 'Ожидает результата'));
    if (progress?.result) item.append(element('p', '', 'Результат: ' + progress.result));
    if (progress?.evidence) item.append(element('p', 'task-plan-criterion', 'Основание: «' + progress.evidence + '»'));
    if (progress?.reason) item.append(element('p', 'task-plan-criterion', progress.reason));
    dom.savedTaskPlan.append(item);
  }
  dom.savedTaskPlan.hidden = plan.length === 0;
  dom.taskPlanEmpty.hidden = plan.length > 0;
  const passed = (taskState.step_results ?? []).filter(result => result.status === 'passed').length;
  dom.taskProgress.textContent = plan.length ? `Подтверждено ${passed} из ${plan.length}` : 'План ещё не утверждён автоматом';
  dom.taskValidation.textContent = taskState.validation
    ? (taskState.validation.allowed ? 'Проверка выполнена. ' : 'Переход заблокирован. ') + taskState.validation.reason
    : 'Проверка ещё не выполнялась.';
  dom.savedTaskStep.textContent = values.current_step || 'Не задан';
  dom.savedTaskAction.textContent = values.expected_action || 'Не задано';
  dom.savedTaskNotes.textContent = values.notes || 'Нет сохранённых заметок';
  dom.taskStageBadge.textContent = stageName;
  dom.taskStageBadge.className = 'status-badge task-stage-badge stage-' + String(taskState.stage ?? 'unknown');
  dom.taskPauseBadge.textContent = done ? 'Задача завершена' : paused ? 'На паузе' : 'Активна';
  dom.taskPauseBadge.className = 'lock-badge ' + (done ? 'locked done' : paused ? 'paused' : 'locked');
  updateTaskStateHelp(taskState);
}

function renderWorkspace(state) {
  const workspace = state.workspace ?? {};
  const active = workspace.active_dialogue ?? {};
  const dialogues = Array.isArray(workspace.dialogues) ? workspace.dialogues : [];
  const tasks = Array.isArray(workspace.tasks) ? workspace.tasks : [];
  const previous = dom.dialogueSelect.value;
  const options = dialogues.map(dialogue => {
    const task = tasks.find(item => String(item.id) === String(dialogue.task_id));
    const taskSuffix = task?.name ? ' · ' + task.name : '';
    return option(dialogue.id, dialogue.name + taskSuffix + ' · ' + (modeNames[dialogue.mode] ?? dialogue.mode));
  });
  dom.dialogueSelect.replaceChildren(...options);
  const selectedId = active.id ?? previous;
  if (dialogues.some(item => String(item.id) === String(selectedId))) dom.dialogueSelect.value = String(selectedId);
  dom.dialogueCount.textContent = integer(dialogues.length);

  const activeTask = tasks.find(item => String(item.id) === String(active.task_id));
  dom.activeTitle.textContent = active.name || 'Диалог не выбран';
  dom.conversationTitle.textContent = active.name || 'Разговор об арте';
  dom.activeTask.textContent = activeTask
    ? 'Задача: ' + activeTask.name + (activeTask.project ? ' · проект: ' + activeTask.project : '')
    : 'Задача не определена';
  dom.activeMode.textContent = modeNames[active.mode] ?? active.mode ?? '—';
  dom.dialogueMode.value = active.mode && modeNames[active.mode] ? active.mode : 'sliding';
  dom.branchPanel.hidden = active.mode !== 'branching';
}

function messageNode(message, animate) {
  const role = message.role === 'assistant' ? 'assistant' : 'user';
  const article = element('article', 'message ' + role + (animate ? ' new-message' : ''));
  article.dataset.messageId = String(message.id ?? '');
  const heading = element('div', 'message-meta');
  heading.append(
    element('span', 'message-role', role === 'assistant' ? 'ПОМОЩНИК' : 'ВЫ'),
    element('span', 'message-id', message.id !== undefined ? '#' + message.id : ''),
  );
  article.append(heading, element('div', 'message-text', message.content ?? ''));
  if (message.request_id !== null && message.request_id !== undefined) {
    const traceButton = element('button', 'message-trace', 'Показать контекст ответа');
    traceButton.type = 'button';
    traceButton.dataset.action = 'trace';
    traceButton.dataset.requestId = String(message.request_id);
    article.append(traceButton);
  }
  return article;
}

function renderConversation(messages) {
  const list = Array.isArray(messages) ? messages : [];
  const fragment = document.createDocumentFragment();
  if (!list.length) {
    const empty = element('div', 'empty-state');
    empty.append(
      element('span', 'empty-symbol', '◌'),
      element('h3', '', 'Начните с конкретной задачи'),
      element('p', '', 'Например, спросите, как отделить форму от материала светом или почему roughness не даёт ожидаемого блика.'),
    );
    fragment.append(empty);
  } else {
    for (const message of list) {
      const id = String(message.id ?? '');
      fragment.append(messageNode(message, knownMessageIds.size > 0 && !knownMessageIds.has(id)));
    }
  }
  dom.conversation.replaceChildren(fragment);
  knownMessageIds = new Set(list.map(message => String(message.id ?? '')));
}

function shortItem(label, text, metadata = '') {
  const card = element('article', 'short-card');
  card.append(element('p', 'memory-key', label), element('p', 'short-text', text));
  if (metadata) card.append(element('p', 'memory-meta', metadata));
  return card;
}

function renderShortMemory(state) {
  const messages = Array.isArray(state.messages) ? state.messages : [];
  const factsObject = state.memory?.facts;
  const mode = activeWorkspace(state)?.mode;
  const fragment = document.createDocumentFragment();
  if (messages.length) {
    const last = messages[messages.length - 1];
    fragment.append(shortItem(
      integer(messages.length) + ' сообщений',
      last?.content ?? '',
      'Последняя реплика · ' + (last?.role === 'assistant' ? 'помощник' : 'пользователь'),
    ));
  }
  let factsCount = 0;
  if (mode === 'facts' && factsObject && typeof factsObject === 'object') {
    const entries = Array.isArray(factsObject) ? factsObject.map((value, index) => [String(index + 1), value]) : Object.entries(factsObject);
    factsCount = entries.length;
    for (const [key, value] of entries) {
      fragment.append(shortItem('Fact · ' + key, typeof value === 'string' ? value : jsonText(value)));
    }
  }
  if (!messages.length && !factsCount) fragment.append(emptyCopy('Диалог пока ничего не сохранил.'));
  dom.shortList.replaceChildren(fragment);
  dom.shortCount.textContent = integer(messages.length + factsCount);
}

function memoryMoveSelect(entry) {
  const select = element('select', 'move-select');
  select.setAttribute('aria-label', 'Куда перенести запись «' + String(entry.key ?? '') + '»');
  const choices = [
    ['working|context|task', 'Рабочая · контекст задачи'],
    ['long_term|decision|project', 'Долговременная · решение проекта'],
    ['long_term|knowledge|project', 'Долговременная · знание проекта'],
  ];
  select.replaceChildren(...choices.map(([value, label]) => option(value, label)));
  const exact = [entry.layer, entry.category, entry.scope].join('|');
  if (choices.some(([value]) => value === exact)) select.value = exact;
  return select;
}

function memoryCard(entry) {
  const managedPreference = entry.category === 'profile' || String(entry.key ?? '').startsWith('personalization.');
  const card = element('article', 'memory-card');
  if (managedPreference) card.classList.add('managed-preference');
  card.dataset.memoryId = String(entry.id);
  const top = element('div', 'memory-card-top');
  const title = element('div');
  title.append(
    element('p', 'memory-key', entry.key || 'Без ключа'),
    element('p', 'memory-revision', 'Версия ' + integer(entry.revision) + ' · ' + (categoryNames[entry.category] ?? entry.category ?? 'категория не указана')),
  );
  const lock = element('span', entry.locked ? 'lock-badge locked' : 'lock-badge', entry.locked ? 'Закреплено' : 'Не закреплено');
  top.append(title, lock);
  card.append(top, element('p', 'memory-value', entry.value ?? ''));

  const facts = element('dl', 'memory-details');
  facts.append(
    detailPair('Область', scopeNames[entry.scope] ?? entry.scope ?? '—'),
    detailPair('Источник', entry.source === 'manual' ? 'ручное сохранение' : 'автоматическое извлечение'),
    detailPair('Причина', entry.reason || 'не указана'),
  );
  if (entry.evidence) facts.append(detailPair('Цитата', '«' + entry.evidence + '»'));
  if (entry.source_ref) facts.append(detailPair('Вызов', entry.source_ref));
  if (entry.created_at) facts.append(detailPair('Создано', dateTime(entry.created_at)));
  card.append(facts);

  if (managedPreference) {
    card.append(element('p', 'managed-preference-note', 'Эта запись управляется редактором выбранного профиля выше. Общие действия памяти для неё недоступны.'));
    return card;
  }

  const moveRow = element('div', 'move-row');
  const moveSelect = memoryMoveSelect(entry);
  const moveButton = element('button', 'small-button', 'Перенести');
  moveButton.type = 'button';
  moveButton.dataset.action = 'move-memory';
  moveButton.dataset.memoryId = String(entry.id);
  moveRow.append(moveSelect, moveButton);
  card.append(moveRow);

  const actions = element('div', 'memory-actions');
  const edit = element('button', 'text-button', entry.locked ? 'Исправить' : 'Исправить и закрепить');
  edit.type = 'button';
  edit.dataset.action = 'edit-memory';
  edit.dataset.memoryId = String(entry.id);
  actions.append(edit);
  if (entry.locked) {
    const unlock = element('button', 'text-button', 'Снять закрепление');
    unlock.type = 'button';
    unlock.dataset.action = 'unlock-memory';
    unlock.dataset.memoryId = String(entry.id);
    actions.append(unlock);
  }
  const remove = element('button', 'text-button danger-button', 'Удалить из будущего контекста');
  remove.type = 'button';
  remove.dataset.action = 'delete-memory';
  remove.dataset.memoryId = String(entry.id);
  actions.append(remove);
  card.append(actions);
  return card;
}

function detailPair(term, description) {
  const wrapper = element('div');
  wrapper.append(element('dt', '', term), element('dd', '', description));
  return wrapper;
}

function renderMemoryLayer(target, entries, emptyText) {
  const list = Array.isArray(entries) ? entries : [];
  target.replaceChildren(...(list.length ? list.map(memoryCard) : [emptyCopy(emptyText)]));
}

function renderMemory(state) {
  renderShortMemory(state);
  const layers = state.workspace?.layers ?? {};
  const working = Array.isArray(layers.working) ? layers.working : [];
  const longTerm = Array.isArray(layers.long_term) ? layers.long_term : [];
  renderMemoryLayer(dom.workingList, working, 'Рабочих записей этой задачи пока нет.');
  renderMemoryLayer(dom.longList, longTerm, 'Подходящих записей пользователя или проекта пока нет.');
  dom.workingCount.textContent = integer(working.length);
  dom.longCount.textContent = integer(longTerm.length);
  dom.memoryCount.textContent = integer(working.length + longTerm.length);
}

function renderAccounting(state) {
  const summary = state.summary ?? {};
  const tokens = state.token_accounting ?? {};
  dom.chatCost.textContent = money(summary.known_cost_usd);
  dom.chatTokens.textContent = integer(tokens.known_total_tokens ?? tokens.total_tokens);
  dom.chatCalls.textContent = integer(summary.api_requests);
  const unknown = Number(summary.unknown_cost_requests ?? 0);
  dom.costNote.textContent = summary.cost_complete
    ? 'полная по сохранённым вызовам'
    : 'неполная · неизвестно: ' + integer(unknown) + ' выз.';
}

function requestCard(request) {
  const card = element('article', 'request-card' + (request.status === 'ok' ? '' : ' problem'));
  const top = element('div', 'request-top');
  const label = element('div');
  label.append(
    element('p', 'request-kind', requestKind(request)),
    element('p', 'request-id', 'Запрос #' + String(request.id ?? '—')),
  );
  top.append(label, element('span', 'request-status status-' + String(request.status ?? ''), statusNames[request.status] ?? request.status ?? 'неизвестно'));
  card.append(top);

  if (request.text) card.append(element('p', 'request-text', request.text));
  const usage = request.usage;
  const noCall = request.usage_status === 'not_requested';
  const metrics = element('dl', 'request-metrics');
  metrics.append(
    detailPair('Input', noCall ? 'нет вызова' : integer(usage?.input_tokens)),
    detailPair('Output', noCall ? '—' : integer(usage?.output_tokens)),
    detailPair('Всего', noCall ? '—' : integer(usage?.total_tokens)),
    detailPair('Стоимость вызова', noCall ? 'нет вызова' : money(request.cost_usd)),
  );
  card.append(metrics);
  card.append(element('p', 'policy-line', 'Политики: вход — ' + policyText(request.input_policy) + ' · выход — ' + policyText(request.output_policy)));
  const context = request.metadata?.context;
  if (context && typeof context === 'object') {
    const included = [];
    if (context.profile_id !== undefined) included.push('профиль: #' + context.profile_id);
    if (Array.isArray(context.profile_refs)) included.push('версии профиля: ' + integer(context.profile_refs.length));
    if (context.use_working !== undefined) included.push('рабочая: ' + (context.use_working ? 'да' : 'нет'));
    if (context.use_long_term !== undefined) included.push('долговременная: ' + (context.use_long_term ? 'да' : 'нет'));
    if (included.length) card.append(element('p', 'context-line', included.join(' · ')));
  }
  const trace = element('button', 'small-button trace-button', 'Показать контекст');
  trace.type = 'button';
  trace.dataset.action = 'trace';
  trace.dataset.requestId = String(request.id);
  card.append(trace);
  return card;
}

function renderRequests(requests) {
  const list = Array.isArray(requests) ? requests.slice().reverse() : [];
  dom.requestCount.textContent = integer(list.length);
  dom.requestList.replaceChildren(...(list.length ? list.map(requestCard) : [emptyCopy('Вызовов API пока нет.') ]));
}

function renderBranches(state) {
  const checkpoints = Array.isArray(state.checkpoints) ? state.checkpoints : [];
  const branches = Array.isArray(state.branches) ? state.branches : [];
  const previousCheckpoint = dom.checkpointSelect.value;
  dom.checkpointSelect.replaceChildren(...checkpoints.map(item => option(item.id, item.name + ' · #' + item.id)));
  if (checkpoints.some(item => String(item.id) === previousCheckpoint)) dom.checkpointSelect.value = previousCheckpoint;
  dom.branchSelect.replaceChildren(...branches.map(item => option(item.id, item.name + (String(item.id) === String(state.active_branch) ? ' · активна' : ''))));
  if (branches.some(item => String(item.id) === String(state.active_branch))) dom.branchSelect.value = String(state.active_branch);
  const active = branches.find(item => String(item.id) === String(state.active_branch));
  dom.activeBranch.textContent = active ? 'Открыта: ' + active.name : 'Нет активной ветки';
}

function renderState(state, options = {}) {
  if (!state || !Array.isArray(state.messages) || !Array.isArray(state.requests) || !state.workspace) {
    throw new Error('Сервер вернул неполное состояние приложения.');
  }
  const active = state.workspace.active_dialogue;
  const nextIdentity = active
    ? String(selectedProfileId(state) ?? 'profile') + ':' + String(active.task_id) + ':' + String(active.id)
    : null;
  const identityChanged = activeDialogueIdentity !== null && nextIdentity !== activeDialogueIdentity;
  activeDialogueIdentity = nextIdentity;
  currentState = state;
  if (identityChanged) resetDialogueUi();
  renderPersonalization(state, {force: options.forceProfileDraft});
  renderWorkspace(state);
  renderTaskState(state);
  renderConversation(state.messages);
  renderMemory(state);
  renderAccounting(state);
  renderRequests(state.requests);
  renderBranches(state);
  dom.bootStatus.textContent = state.boot_id ? 'Запуск ' + String(state.boot_id).slice(0, 8) : 'Состояние сохранено';
  dom.sessionInfo.textContent = 'Диалог #' + String(activeWorkspace(state)?.id ?? '—') + ' · профиль ' + String(state.personalization?.profile?.name ?? state.workspace.user_id ?? 'local');
  if (options.scroll) requestAnimationFrame(() => dom.conversation.scrollTo({top: dom.conversation.scrollHeight, behavior: 'smooth'}));
  syncControls();
}

async function loadState(options = {}) {
  if (busy) return;
  ready = false;
  setBusy(true, 'Загрузка');
  setNotice('');
  try {
    const {response, body} = await fetchJson('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error(errorText(body, 'Не удалось загрузить рабочее пространство.'));
    renderState(body, {forceProfileDraft: options.discardProfileDraft === true});
    ready = true;
    dom.agentStatus.textContent = 'Готов к вопросу';
  } catch (error) {
    currentState = null;
    setNotice(error instanceof TypeError
      ? 'Сервер недоступен. Запустите приложение и обновите состояние.'
      : error.message, 'error');
  } finally {
    setBusy(false);
  }
}

async function mutation(url, payload, workingLabel, successText, options = {}) {
  if (busy || !ready) return null;
  setBusy(true, workingLabel);
  setNotice('');
  try {
    const {response, body} = await fetchJson(url, {
      method: options.method ?? 'POST',
      headers: {'Content-Type': 'application/json'},
      body: options.method === 'DELETE' ? undefined : JSON.stringify(payload ?? {}),
    });
    if (body.state) renderState(body.state, {scroll: options.scroll});
    if (!response.ok || (body.status && body.status !== 'ok')) {
      throw new Error(errorText(body, 'Операция не выполнена.'));
    }
    if (typeof options.afterSuccess === 'function') options.afterSuccess(body);
    if (successText) setNotice(successText, 'success');
    return body;
  } catch (error) {
    setNotice(error instanceof TypeError
      ? 'Соединение прервалось. Результат мог сохраниться: обновите состояние перед повтором.'
      : error.message, 'error');
    if (error instanceof TypeError) ready = false;
    return null;
  } finally {
    setBusy(false);
  }
}

async function taskStateAction(action, successText = '') {
  const identity = activeRequestIdentity();
  if (!Number.isFinite(identity.task_id) || !Number.isFinite(identity.profile_id)) {
    setNotice('Не удалось определить задачу или профиль. Обновите состояние.', 'error');
    return null;
  }
  const labels = {
    pause: 'Ставим задачу на паузу',
    resume: 'Продолжаем задачу',
  };
  return mutation('/api/task-state/action', {...identity, action}, labels[action] ?? 'Обновляем состояние', successText);
}

function updateMemoryEditorOptions() {
  const layer = dom.memoryLayer.value;
  if (layer === 'working') {
    dom.memoryCategory.replaceChildren(option('context', 'Контекст задачи'));
    dom.memoryScope.replaceChildren(option('task', 'Текущая задача'));
  } else {
    const previousCategory = dom.memoryCategory.value;
    const previousScope = dom.memoryScope.value;
    dom.memoryCategory.replaceChildren(
      option('decision', 'Принятое решение'),
      option('knowledge', 'Знание'),
    );
    dom.memoryScope.replaceChildren(option('user', 'Пользователь local'), option('project', 'Текущий проект'));
    if (['decision', 'knowledge'].includes(previousCategory)) dom.memoryCategory.value = previousCategory;
    if (['user', 'project'].includes(previousScope)) dom.memoryScope.value = previousScope;
  }
}

function allMemoryEntries() {
  const layers = currentState?.workspace?.layers ?? {};
  return [...(layers.working ?? []), ...(layers.long_term ?? [])];
}

function resetMemoryEditor() {
  dom.memoryEditId.value = '';
  dom.memoryForm.reset();
  dom.memoryLayer.value = 'working';
  updateMemoryEditorOptions();
  dom.memoryCancel.hidden = true;
  $('#save-memory').textContent = 'Сохранить и закрепить';
  syncControls();
}

function editMemory(entry) {
  dom.memoryEditId.value = String(entry.id);
  dom.memoryLayer.value = entry.layer === 'long_term' ? 'long_term' : 'working';
  updateMemoryEditorOptions();
  if ([...dom.memoryCategory.options].some(item => item.value === entry.category)) dom.memoryCategory.value = entry.category;
  if ([...dom.memoryScope.options].some(item => item.value === entry.scope)) dom.memoryScope.value = entry.scope;
  dom.memoryKey.value = entry.key ?? '';
  dom.memoryValue.value = entry.value ?? '';
  dom.memoryReason.value = entry.reason ?? '';
  dom.memoryCancel.hidden = false;
  $('#save-memory').textContent = 'Сохранить исправление и закрепить';
  dom.memoryEditor.open = true;
  dom.memoryEditor.scrollIntoView({behavior: 'smooth', block: 'center'});
  dom.memoryValue.focus({preventScroll: true});
  syncControls();
}

function previewText(metrics) {
  const labels = {
    instructions_tokens_estimate: 'инструкции',
    working_memory_tokens_estimate: 'рабочая память',
    long_term_memory_tokens_estimate: 'долговременная память',
    facts_tokens_estimate: 'facts диалога',
    history_tokens_estimate: 'история',
    new_message_tokens_estimate: 'новый вопрос',
    context_tokens_estimate: 'контекст',
    input_text_tokens_estimate: 'весь текст входа',
  };
  const parts = [];
  for (const [key, label] of Object.entries(labels)) {
    if (metrics?.[key] !== null && metrics?.[key] !== undefined) parts.push(label + ': ≈ ' + integer(metrics[key]));
  }
  return parts.length ? parts.join(' · ') + '. Точный расход появится после API-вызова.' : jsonText(metrics);
}

async function showPreview() {
  const prompt = dom.prompt.value.trim();
  if (!prompt || busy || !ready) return;
  setBusy(true, 'Собираем состав');
  setNotice('');
  try {
    const {response, body} = await fetchJson('/api/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ...activeRequestIdentity(),
        prompt,
        use_working: dom.useWorking.checked,
        use_long_term: dom.useLongTerm.checked,
      }),
    });
    if (!response.ok || body.status !== 'ok') throw new Error(errorText(body, 'Состав запроса недоступен.'));
    dom.previewResult.textContent = previewText(body.token_metrics);
    dom.previewResult.hidden = false;
  } catch (error) {
    setNotice(error instanceof TypeError ? 'Не удалось получить состав запроса.' : error.message, 'error');
  } finally {
    setBusy(false);
  }
}

function traceEntry(entry) {
  const card = element('article', 'trace-memory-card');
  card.append(
    element('p', 'memory-key', entry.key ?? 'Запись #' + String(entry.id ?? '—')),
    element('p', 'memory-value', entry.value ?? ''),
    element('p', 'memory-meta', 'ID ' + String(entry.id ?? '—') + ' · версия ' + String(entry.revision ?? '—') + ' · ' + (entry.layer === 'long_term' ? 'долговременная' : 'рабочая')),
  );
  return card;
}

function traceProfileEntries(body) {
  if (Array.isArray(body.profile)) return body.profile;
  if (Array.isArray(body.profile?.records)) return body.profile.records;
  if (body.profile && typeof body.profile === 'object') return [body.profile];
  return [];
}

function traceProfileCard(entry) {
  if (entry.key !== undefined || entry.value !== undefined) return traceEntry(entry);
  const card = element('article', 'trace-memory-card trace-profile-card');
  const values = profileValues(entry);
  card.append(
    element('p', 'memory-key', entry.name || 'Профиль #' + String(entry.id ?? '—')),
    element('p', 'memory-value', 'Стиль: ' + (values.style || 'не задан') + '\nФормат: ' + formatNames[values.format] + '\nОграничения: ' + (values.constraints || 'не заданы')),
  );
  return card;
}

async function loadTrace(requestId) {
  if (!requestId || busy || !ready) return;
  setBusy(true, 'Загрузка трассы');
  setNotice('');
  try {
    const {response, body} = await fetchJson('/api/trace/' + encodeURIComponent(requestId), {cache: 'no-store'});
    if (!response.ok || body.status !== 'ok') throw new Error(errorText(body, 'Трасса недоступна.'));
    const fragment = document.createDocumentFragment();
    const context = element('section', 'trace-section');
    context.append(element('h4', '', 'Метаданные контекста'), element('pre', 'trace-json', jsonText(body.context ?? {})));
    fragment.append(context);
    const profile = traceProfileEntries(body);
    const profileSection = element('section', 'trace-section');
    const tracedProfileId = body.context?.profile_id ?? body.metadata?.profile_id ?? '—';
    profileSection.append(element('h4', '', 'Настройки профиля #' + String(tracedProfileId) + ' в этом запросе'));
    profileSection.append(...(profile.length
      ? profile.map(traceProfileCard)
      : [emptyCopy('Сервер не вернул версии профиля для этого вызова.') ]));
    fragment.append(profileSection);
    const memory = Array.isArray(body.memory) ? body.memory : [];
    const memorySection = element('section', 'trace-section');
    memorySection.append(element('h4', '', 'Версии памяти · ' + integer(memory.length)));
    memorySection.append(...(memory.length ? memory.map(traceEntry) : [emptyCopy('Явная память в этот запрос не вошла.') ]));
    fragment.append(memorySection);
    const selection = element('section', 'trace-section');
    selection.append(element('h4', '', 'Выбор сообщений и слоёв'), element('pre', 'trace-json', jsonText(body.selection ?? {})));
    fragment.append(selection);
    const metadata = element('section', 'trace-section');
    metadata.append(element('h4', '', 'Метаданные вызова'), element('pre', 'trace-json', jsonText(body.metadata ?? {})));
    fragment.append(metadata);
    dom.traceContent.replaceChildren(fragment);
    dom.traceStatus.textContent = 'Запрос #' + requestId;
    dom.tracePanel.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  } catch (error) {
    setNotice(error instanceof TypeError ? 'Не удалось загрузить трассу.' : error.message, 'error');
  } finally {
    setBusy(false);
  }
}

dom.askForm.addEventListener('submit', async event => {
  event.preventDefault();
  const prompt = dom.prompt.value.trim();
  if (!prompt) return;
  const body = await mutation('/api/ask', {
    ...activeRequestIdentity(),
    prompt,
    use_working: dom.useWorking.checked,
    use_long_term: dom.useLongTerm.checked,
  }, 'Агент работает', '', {scroll: true});
  if (body?.status === 'ok') {
    dom.prompt.value = '';
    dom.previewResult.hidden = true;
    dom.previewResult.textContent = '';
    updatePromptCount();
    setNotice('Ответ готов. Автоматическое распределение и все расходы сохранены.', 'success');
  }
});

dom.toggleTaskPause.addEventListener('click', async () => {
  const paused = currentState?.task_state?.paused === true;
  await taskStateAction(
    paused ? 'resume' : 'pause',
    paused
      ? 'Задача продолжена. Вопрос остался в поле; ничего не отправлено автоматически.'
      : 'Задача поставлена на паузу. Чат заблокирован до продолжения.',
  );
});

dom.prompt.addEventListener('input', updatePromptCount);
dom.preview.addEventListener('click', showPreview);
dom.refresh.addEventListener('click', () => {
  if (!confirmProfileDraftLoss('Обновление состояния')) return;
  loadState({discardProfileDraft: true});
});

dom.profileSelect.addEventListener('change', async () => {
  const profileId = Number(dom.profileSelect.value);
  const previousId = selectedProfileId();
  if (!profileId || String(profileId) === String(previousId)) return;
  if (!confirmProfileDraftLoss('Переключение профиля')) {
    dom.profileSelect.value = String(previousId ?? '');
    return;
  }
  knownMessageIds = new Set();
  const body = await mutation('/api/profiles/select', {profile_id: profileId}, 'Переключаем профиль', 'Профиль выбран. Загружены только его задачи, диалоги, память и расходы.', {scroll: true});
  if (!body) dom.profileSelect.value = String(previousId ?? '');
});

dom.profileForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!profileDirty) return;
  await mutation('/api/profile', profileDraft(), 'Сохраняем профиль', 'Настройки сохранены и будут переданы следующему ответу.', {
    method: 'PUT',
    afterSuccess: body => {
      setProfileDirty(false);
      if (body.state) renderPersonalization(body.state, {force: true});
    },
  });
});

dom.profileCreateForm.addEventListener('submit', async event => {
  event.preventDefault();
  const name = dom.profileName.value.trim();
  if (!name || !confirmProfileDraftLoss('Создание нового профиля')) return;
  knownMessageIds = new Set();
  await mutation('/api/profiles', {name, style: '', format: 'plain', constraints: ''}, 'Создаём профиль', 'Нейтральный профиль создан и выбран.', {
    afterSuccess: () => {
      dom.profileName.value = '';
      syncControls();
    },
    scroll: true,
  });
});

document.querySelectorAll('[data-profile-example]').forEach(button => {
  button.addEventListener('click', async () => {
    const example = profileExamples[button.dataset.profileExample];
    if (!example || !confirmProfileDraftLoss('Создание демонстрационного профиля')) return;
    const payload = {...example, name: availableProfileName(example.name)};
    knownMessageIds = new Set();
    await mutation('/api/profiles', payload, 'Создаём демо-профиль', 'Демонстрационный профиль «' + payload.name + '» создан и выбран.', {scroll: true});
  });
});

dom.openDialogue.addEventListener('click', () => {
  if (!dom.dialogueSelect.value) return;
  knownMessageIds = new Set();
  mutation('/api/open', {dialogue_id: Number(dom.dialogueSelect.value)}, 'Открываем диалог', 'Диалог и его краткосрочная история загружены.', {scroll: true});
});

dom.dialogueForm.addEventListener('submit', event => {
  event.preventDefault();
  const active = activeWorkspace();
  const name = dom.dialogueName.value.trim();
  if (!active || !name) return;
  knownMessageIds = new Set();
  mutation('/api/dialogue', {
    name,
    task_id: Number(active.task_id),
    mode: dom.dialogueMode.value,
  }, 'Создаём диалог', 'Новый диалог открыт. Рабочая память задачи сохранена, история начата заново.', {
    afterSuccess: () => {
      dom.dialogueName.value = '';
      $('#dialogue-create-details').open = false;
    },
  });
});

dom.taskForm.addEventListener('submit', event => {
  event.preventDefault();
  const name = dom.taskName.value.trim();
  const project = dom.taskProject.value.trim();
  if (!name) return;
  knownMessageIds = new Set();
  mutation('/api/task', {name, project, mode: dom.taskMode.value}, 'Создаём задачу', 'Новая задача и пустой диалог открыты.', {
    afterSuccess: () => {
      dom.taskName.value = '';
      dom.taskProject.value = '';
      $('#task-create-details').open = false;
    },
  });
});

dom.memoryLayer.addEventListener('change', updateMemoryEditorOptions);
dom.memoryForm.addEventListener('submit', event => {
  event.preventDefault();
  const payload = {
    layer: dom.memoryLayer.value,
    category: dom.memoryCategory.value,
    scope: dom.memoryScope.value,
    key: dom.memoryKey.value.trim(),
    value: dom.memoryValue.value.trim(),
    reason: dom.memoryReason.value.trim(),
  };
  if (!payload.key || !payload.value || !payload.reason) return;
  mutation('/api/memory', payload, 'Сохраняем память', 'Запись сохранена вручную и закреплена от автоматических изменений.', {
    afterSuccess: resetMemoryEditor,
  });
});
dom.memoryCancel.addEventListener('click', resetMemoryEditor);

document.addEventListener('input', event => {
  if (event.target.matches('#profile-style, #profile-constraints')) {
    const saved = profileValues(currentState?.personalization?.profile);
    const draft = profileDraft();
    setProfileDirty(draft.style !== saved.style || draft.format !== saved.format || draft.constraints !== saved.constraints);
  }
  if (event.target.matches('#profile-name, #dialogue-name, #task-name, #task-project, #memory-key, #memory-value, #memory-reason, #checkpoint-name, #branch-name')) syncControls();
});
dom.profileFormat.addEventListener('change', () => {
  const saved = profileValues(currentState?.personalization?.profile);
  const draft = profileDraft();
  setProfileDirty(draft.style !== saved.style || draft.format !== saved.format || draft.constraints !== saved.constraints);
});
window.addEventListener('beforeunload', event => {
  if (!profileDirty) return;
  event.preventDefault();
  event.returnValue = '';
});
dom.dialogueSelect.addEventListener('change', syncControls);
dom.checkpointSelect.addEventListener('change', syncControls);
dom.branchSelect.addEventListener('change', syncControls);

document.addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (!button || busy || !ready) return;
  const action = button.dataset.action;
  if (action === 'trace') {
    loadTrace(button.dataset.requestId);
    return;
  }
  const memoryId = Number(button.dataset.memoryId);
  const entry = allMemoryEntries().find(item => Number(item.id) === memoryId);
  if (!entry) return;
  if (action === 'edit-memory') {
    editMemory(entry);
  } else if (action === 'unlock-memory') {
    mutation('/api/memory/' + memoryId + '/unlock', {}, 'Снимаем закрепление', 'Закрепление снято: автоматика снова может изменить запись.');
  } else if (action === 'delete-memory') {
    mutation('/api/memory/' + memoryId, null, 'Исключаем запись', 'Запись исключена из будущего контекста; версии сохранены для трассировки.', {method: 'DELETE'});
  } else if (action === 'move-memory') {
    const select = button.closest('.move-row')?.querySelector('select');
    const [layer, category, scope] = String(select?.value ?? '').split('|');
    if (layer && category && scope) {
      mutation('/api/memory/' + memoryId + '/move', {layer, category, scope}, 'Переносим запись', 'Запись перенесена вручную и закреплена в выбранном слое.');
    }
  }
});

dom.checkpointForm.addEventListener('submit', event => {
  event.preventDefault();
  const name = dom.checkpointName.value.trim();
  if (!name) return;
  mutation('/api/checkpoint', {name}, 'Сохраняем checkpoint', 'Checkpoint «' + name + '» сохранён.', {
    afterSuccess: () => { dom.checkpointName.value = ''; },
  });
});

dom.branchForm.addEventListener('submit', event => {
  event.preventDefault();
  const name = dom.branchName.value.trim();
  if (!name || !dom.checkpointSelect.value) return;
  mutation('/api/branch', {checkpoint_id: Number(dom.checkpointSelect.value), name}, 'Создаём ветку', 'Ветка «' + name + '» создана и открыта.', {
    afterSuccess: () => { dom.branchName.value = ''; },
  });
});

dom.switchForm.addEventListener('submit', event => {
  event.preventDefault();
  if (!dom.branchSelect.value) return;
  knownMessageIds = new Set();
  mutation('/api/switch', {branch_id: Number(dom.branchSelect.value)}, 'Открываем ветку', 'Путь ветки восстановлен.', {scroll: true});
});

updateMemoryEditorOptions();
updatePromptCount();
loadState();
