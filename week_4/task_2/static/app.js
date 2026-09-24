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
  dialogueList: $('#dialogue-list'),
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
  taskProgress: $('#task-progress'),
  taskValidation: $('#task-validation'),
  savedTaskStep: $('#saved-task-step'),
  savedTaskAction: $('#saved-task-action'),
  savedTaskNotes: $('#saved-task-notes'),
  toggleTaskPause: $('#toggle-task-pause'),
  taskStateHelp: $('#task-state-help'),
  invariantForm: $('#invariant-form'),
  invariantRules: $('#invariant-rules'),
  invariantSavedStatus: $('#invariant-saved-status'),
  invariantScope: $('#invariant-scope'),
  invariantRevision: $('#invariant-revision'),
  invariantCount: $('#invariant-count'),
  invariantActiveList: $('#invariant-active-list'),
  invariantRuleHelp: $('#invariant-rule-help'),
  saveInvariants: $('#save-invariants'),
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
  knowledgeSources: $('#knowledge-sources'),
  knowledgeQuery: $('#knowledge-query'),
  knowledgeWikiQuery: $('#knowledge-wiki-query'),
  knowledgeLanguage: $('#knowledge-language'),
  knowledgeCommunity: $('#knowledge-community'),
  knowledgeResults: $('#knowledge-results'),
  knowledgeCount: $('#knowledge-count'),
  promptCount: $('#prompt-count'),
  preview: $('#preview-button'),
  previewResult: $('#token-preview'),
  memoryCount: $('#memory-count'),
  shortCount: $('#short-count'),
  workingCount: $('#working-count'),
  longCount: $('#long-count'),
  shortList: $('#memory-short'),
  workingList: $('#memory-working'),
  longList: $('#memory-long'),
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
  branchUnavailable: $('#branch-unavailable'),
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
  validation: 'Проверка этапа',
  invariant_input: 'Проверка правил · вход',
  invariant_output: 'Проверка правил · ответ',
  extraction: 'Извлечение памяти',
  facts: 'Обновление facts',
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

const eventNames = {
  stay: 'продолжить текущий этап',
  plan_ready: 'принять план',
  result_reported: 'принять результаты',
  revise_plan: 'пересмотреть план',
  revise_work: 'вернуться к выполнению',
  finish: 'завершить задачу',
};

const profileExamples = {
  beginner: {
    name: 'Демо · Новичок',
    style: 'Объясняй специальные термины простыми словами при первом употреблении.',
    format: 'steps',
    constraints: 'Давай ровно 3 нумерованных шага. Объясняй на простых примерах композиции, цвета и формы; без привязки к программам.',
  },
  experienced: {
    name: 'Демо · Опытный',
    style: 'Отвечай кратко, без объяснения базовых терминов.',
    format: 'markdown',
    constraints: 'Разбирай художественные принципы и их ограничения; не переходи к настройкам программ без запроса.',
  },
};

let currentState = null;
let busy = false;
let ready = false;
let knownMessageIds = new Set();
let activeDialogueIdentity = null;
let renderedProfileId = null;
let profileDirty = false;
let renderedInvariantIdentity = null;
let invariantDirty = false;

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
  document.dispatchEvent(new CustomEvent('workspace-notice', {detail: {text, type}}));
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
    const help = {
      planning: 'Обсудите план и явно утвердите его в чате. Пока план не утверждён, выполнение не начинается.',
      execution: 'Сообщите конкретные результаты каждого шага. Затем можно перейти к проверке.',
      validation: 'Сопоставьте результаты с критериями и подтвердите итог. Без этого задача не завершится.',
    };
    dom.taskStateHelp.textContent = help[taskState?.stage] ?? 'Обновите состояние задачи.';
  }
}

function syncControls() {
  const disableAll = busy;
  document.querySelectorAll('button, input, textarea, select').forEach(control => {
    control.disabled = disableAll && !control.matches('[data-panel], [data-close-panel], [data-shell-toggle]');
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
  dom.profileStyle.disabled = !ready || !selectedProfile || done;
  dom.profileFormat.disabled = !ready || !selectedProfile || done;
  dom.profileConstraints.disabled = !ready || !selectedProfile || done;
  $('#save-profile').disabled = !ready || !selectedProfile || !profileDirty || done;
  $('#create-profile').disabled = !ready || !dom.profileName.value.trim();
  document.querySelectorAll('[data-profile-example]').forEach(button => { button.disabled = !ready; });
  dom.openDialogue.disabled = !ready || !dom.dialogueSelect.value || (done && !opensOtherTask);
  dom.dialogueList.querySelectorAll('[data-dialogue-id]').forEach(button => {
    const target = dialogues.find(item => String(item.id) === button.dataset.dialogueId);
    const opensOtherTask = target && String(target.task_id) !== String(active?.task_id);
    button.disabled = !ready || !target || (done && !opensOtherTask);
  });
  dom.dialogueName.disabled = !ready || done;
  dom.dialogueMode.disabled = !ready || done;
  $('#create-dialogue').disabled = !ready || !dom.dialogueName.value.trim() || done;
  $('#create-task').disabled = !ready || !dom.taskName.value.trim();
  dom.toggleTaskPause.disabled = !ready || !taskState || done;
  dom.toggleTaskPause.textContent = paused ? 'Продолжить' : 'Поставить на паузу';
  const invariantIdentityValue = invariantIdentity();
  const invariantDraftState = invariantDraftValidation();
  dom.invariantRules.disabled = !ready || !invariantIdentityValue;
  dom.saveInvariants.disabled = !ready || !invariantIdentityValue || !invariantDirty || !invariantDraftState.valid;
  updateTaskStateHelp(taskState);
  dom.prompt.disabled = !ready || taskBlocked;
  dom.knowledgeSources.disabled = !ready || taskBlocked;
  const knowledgeOff = dom.knowledgeSources.value === 'off';
  dom.knowledgeQuery.disabled = !ready || taskBlocked || knowledgeOff;
  dom.knowledgeWikiQuery.disabled = !ready || taskBlocked || !['both', 'wikipedia'].includes(dom.knowledgeSources.value);
  dom.knowledgeLanguage.disabled = !ready || taskBlocked || !['both', 'wikipedia'].includes(dom.knowledgeSources.value);
  dom.knowledgeCommunity.disabled = !ready || taskBlocked || !['both', 'stackexchange'].includes(dom.knowledgeSources.value);
  $('#send').disabled = !ready || !dom.prompt.value.trim() || taskBlocked;
  dom.preview.disabled = !ready || !dom.prompt.value.trim() || taskBlocked;
  dom.checkpointName.disabled = !ready || done;
  dom.checkpointSelect.disabled = !ready || done;
  dom.branchName.disabled = !ready || done;
  dom.branchSelect.disabled = !ready || done;
  $('#create-checkpoint').disabled = !ready || done || !dom.checkpointName.value.trim();
  $('#create-branch').disabled = !ready || done || !dom.checkpointSelect.value || !dom.branchName.value.trim();
  $('#switch-branch').disabled = !ready || done || !dom.branchSelect.value;
  document.querySelectorAll('[data-action="delete-memory"]').forEach(button => { button.disabled = !ready; });
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

function invariantRulesFromState(state = currentState) {
  const rules = Array.isArray(state?.invariants?.rules) ? state.invariants.rules : [];
  return rules.map(rule => typeof rule === 'string' ? rule : rule?.text)
    .filter(rule => typeof rule === 'string');
}

function invariantDraftValidation() {
  const rules = String(dom.invariantRules?.value ?? '')
    .split(/\r?\n/)
    .map(rule => rule.trim())
    .filter(Boolean);
  if (rules.length > 5) return {valid: false, rules, error: 'Можно сохранить не больше 5 правил.'};
  const oversized = rules.findIndex(rule => Array.from(rule).length > 500);
  if (oversized >= 0) return {valid: false, rules, error: 'Правило ' + String(oversized + 1) + ' длиннее 500 символов.'};
  return {valid: true, rules, error: ''};
}

function invariantIdentity(state = currentState) {
  const taskId = state?.invariants?.task_id ?? state?.task_state?.task_id ?? activeWorkspace(state)?.task_id;
  const profileId = selectedProfileId(state);
  if (taskId === null || taskId === undefined || profileId === null || profileId === undefined) return null;
  return String(profileId) + ':' + String(taskId);
}

function setInvariantDirty(value) {
  invariantDirty = Boolean(value);
  dom.invariantSavedStatus.textContent = invariantDirty ? 'Есть несохранённые изменения' : 'Сохранено';
  dom.invariantSavedStatus.classList.toggle('locked', !invariantDirty);
  dom.invariantSavedStatus.classList.toggle('unsaved', invariantDirty);
  syncControls();
}

function updateInvariantRuleHelp() {
  const draft = invariantDraftValidation();
  dom.invariantRuleHelp.classList.toggle('validation-error', !draft.valid);
  dom.invariantRuleHelp.textContent = draft.valid
    ? 'Черновик: ' + integer(draft.rules.length) + ' из 5. Каждое правило — до 500 символов; пустой список удалит действующие правила после сохранения.'
    : draft.error;
  return draft;
}

function updateInvariantDirty() {
  const saved = invariantRulesFromState();
  const draft = updateInvariantRuleHelp().rules;
  setInvariantDirty(saved.length !== draft.length || saved.some((rule, index) => rule !== draft[index]));
}

function confirmDraftLoss(actionText, options = {}) {
  const includeProfile = options.profile !== false;
  const includeInvariants = options.invariants !== false;
  const dirty = [];
  if (includeProfile && profileDirty) dirty.push('профиля');
  if (includeInvariants && invariantDirty) dirty.push('правил задачи');
  if (!dirty.length) return true;
  return window.confirm('Есть несохранённый черновик ' + dirty.join(' и ') + '. ' + actionText + ' отбросит его. Продолжить?');
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
  const profileLaunchName = $('#profile-launch-name');
  if (profileLaunchName) profileLaunchName.textContent = profile?.name || 'Профиль';
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

function invariantSnapshot(source) {
  const snapshot = source?.invariants ?? source?.context?.invariants;
  return snapshot && Array.isArray(snapshot.rules) ? snapshot : null;
}

function invariantVerdictText(check) {
  if (!check || !Array.isArray(check.checks)) return '';
  const phase = check.phase === 'input' ? 'вход' : check.phase === 'output' ? 'ответ' : String(check.phase ?? 'этап не указан');
  const verdicts = check.checks.map(item => String(item.id ?? '—') + ': ' + (item.violated === true ? 'нарушено' : item.violated === false ? 'соблюдено' : 'нет вердикта'));
  return 'Проверка (' + phase + '): ' + verdicts.join(' · ');
}

function safeTraceMetadata(value) {
  if (Array.isArray(value)) return value.map(safeTraceMetadata);
  if (!value || typeof value !== 'object') return value;
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    if (/candidate|draft|raw.*output|rejected.*(?:text|answer|response)|generated.*(?:text|answer|response)/i.test(key)) continue;
    result[key] = safeTraceMetadata(item);
  }
  return result;
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
  const approval = taskState.plan_approval;
  $('#task-plan-approval').textContent = approval
    ? 'Пользователь утвердил текущий план: «' + approval.evidence + '»'
    : 'Ожидается явное утверждение показанного плана в чате.';
  const plan = Array.isArray(taskState.plan) ? taskState.plan : [];
  dom.savedTaskPlan.replaceChildren();
  const stepResults = Array.isArray(taskState.step_results) ? taskState.step_results : [];
  const resultsByStep = new Map(stepResults
    .filter(result => result && Number.isInteger(result.step))
    .map(result => [result.step, result]));
  const statusNames = {
    pending: 'Ожидает результата',
    passed: 'Критерий выполнен',
    failed: 'Критерий не выполнен',
  };
  for (const [index, step] of plan.entries()) {
    const item = document.createElement('li');
    const action = document.createElement('p');
    const criterion = document.createElement('p');
    action.textContent = typeof step?.action === 'string' ? step.action : '';
    criterion.className = 'task-plan-criterion';
    criterion.textContent = 'Проверка: ' + (typeof step?.criterion === 'string' ? step.criterion : 'не указана');
    item.append(action, criterion);
    const progress = resultsByStep.get(index + 1);
    const progressStatus = Object.prototype.hasOwnProperty.call(statusNames, progress?.status)
      ? progress.status
      : 'pending';
    item.append(element('p', 'task-step-status status-' + progressStatus, statusNames[progressStatus]));
    if (typeof progress?.result === 'string' && progress.result) {
      item.append(element('p', 'task-step-result', 'Результат: ' + progress.result));
    }
    if (typeof progress?.evidence === 'string' && progress.evidence) {
      item.append(element('p', 'task-plan-criterion', 'Основание: «' + progress.evidence + '»'));
    }
    if (typeof progress?.reason === 'string' && progress.reason) {
      item.append(element('p', 'task-plan-criterion', 'Причина: ' + progress.reason));
    }
    dom.savedTaskPlan.append(item);
  }
  dom.savedTaskPlan.hidden = plan.length === 0;
  dom.taskPlanEmpty.hidden = plan.length > 0;
  const passed = stepResults.filter(result => result?.status === 'passed').length;
  dom.taskProgress.textContent = plan.length
    ? 'Подтверждено ' + integer(passed) + ' из ' + integer(plan.length)
    : 'План ещё не составлен';
  const validation = taskState.validation && typeof taskState.validation === 'object'
    ? taskState.validation
    : null;
  if (validation) {
    const event = eventNames[validation.event] ?? String(validation.event ?? 'неизвестный переход');
    const verdict = validation.event === 'stay' ? 'Результаты проверены; этап сохранён.'
      : validation.allowed === true ? 'Переход разрешён.' : 'Переход заблокирован.';
    const reason = typeof validation.reason === 'string' && validation.reason
      ? ' ' + validation.reason
      : '';
    dom.taskValidation.textContent = 'Событие: ' + event + '. ' + verdict + reason;
  } else {
    dom.taskValidation.textContent = 'Проверка ещё не выполнялась.';
  }
  dom.savedTaskStep.textContent = values.current_step || 'Не задан';
  dom.savedTaskAction.textContent = values.expected_action || 'Не задано';
  dom.savedTaskNotes.textContent = values.notes || 'Нет сохранённых заметок';
  dom.taskStageBadge.textContent = stageName;
  dom.taskStageBadge.className = 'status-badge task-stage-badge stage-' + String(taskState.stage ?? 'unknown');
  dom.taskPauseBadge.textContent = done ? 'Задача завершена' : paused ? 'На паузе' : 'Активна';
  dom.taskPauseBadge.className = 'lock-badge ' + (done ? 'locked done' : paused ? 'paused' : 'locked');

  updateTaskStateHelp(taskState);
}

function renderInvariants(state, options = {}) {
  const invariants = state?.invariants;
  if (!invariants || !Array.isArray(invariants.rules)) {
    throw new Error('Сервер не вернул правила текущей задачи.');
  }
  const identity = invariantIdentity(state);
  const identityChanged = renderedInvariantIdentity !== null && identity !== renderedInvariantIdentity;
  const profile = state.personalization?.profile;
  const tasks = Array.isArray(state.workspace?.tasks) ? state.workspace.tasks : [];
  const task = tasks.find(item => String(item.id) === String(invariants.task_id));
  const rules = invariants.rules.filter(rule => rule && typeof rule.text === 'string');

  dom.invariantScope.textContent = 'Профиль: ' + (profile?.name || '#' + String(selectedProfileId(state) ?? '—'))
    + ' · задача: ' + (task?.name || '#' + String(invariants.task_id ?? '—'));
  dom.invariantRevision.textContent = 'Ревизия ' + integer(invariants.revision);
  dom.invariantCount.textContent = integer(rules.length);
  dom.invariantActiveList.replaceChildren(...(rules.length
    ? rules.map((rule, index) => {
        const card = element('article', 'invariant-rule-card');
        card.append(
          element('span', 'invariant-rule-id', rule.id || 'R' + String(index + 1)),
          element('p', 'invariant-rule-text', rule.text),
        );
        return card;
      })
    : [emptyCopy('Дополнительных правил нет. Ответ проходит по обычному пути без двух смысловых проверок.') ]));

  if (options.force || identityChanged || !invariantDirty) {
    dom.invariantRules.value = rules.map(rule => rule.text).join('\n');
    renderedInvariantIdentity = identity;
    updateInvariantRuleHelp();
    setInvariantDirty(false);
  }
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
  const items = dialogues.map(dialogue => {
    const task = tasks.find(item => String(item.id) === String(dialogue.task_id));
    const button = element('button', 'dialogue-item');
    button.type = 'button';
    button.dataset.dialogueId = String(dialogue.id);
    button.append(
      element('span', 'dialogue-item-title', dialogue.name || 'Без названия'),
      element('span', 'dialogue-item-task', task?.name || 'Задача #' + String(dialogue.task_id ?? '—')),
    );
    if (String(dialogue.id) === String(active.id)) {
      button.classList.add('is-active');
      button.setAttribute('aria-current', 'page');
    }
    return button;
  });
  dom.dialogueList.replaceChildren(...(items.length ? items : [emptyCopy('Диалогов пока нет.') ]));
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
  dom.branchUnavailable.hidden = active.mode === 'branching';
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
      element('p', '', 'На low-poly модели с PBR-материалом после подключения normal map появились заметные швы по границам UV, а на зеркальных участках освещение выглядит неправильным. Без normal map этих артефактов нет.'),
      element('p', '', 'Как шейдер преобразует нормаль из текстуры в направление для расчёта освещения? Хочу разобраться в tangent space и понять, как последовательно отличить проблему запекания от ошибки импорта текстуры или расчёта тангентов.'),
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

function isReferenceMemory(entry) {
  return entry?.layer === 'long_term'
    && entry.category !== 'profile'
    && !String(entry.key ?? '').startsWith('personalization.');
}

function memoryCard(entry) {
  const card = element('article', 'memory-card');
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

  if (isReferenceMemory(entry)) {
    const actions = element('div', 'memory-actions');
    const remove = element('button', 'text-button danger-button', 'Забыть');
    remove.type = 'button';
    remove.dataset.action = 'delete-memory';
    remove.dataset.memoryId = String(entry.id);
    actions.append(remove);
    card.append(actions);
  }
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
  const longTerm = Array.isArray(layers.long_term) ? layers.long_term.filter(isReferenceMemory) : [];
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
  const kind = request?.metadata?.kind ?? request?.kind ?? 'answer';
  const top = element('div', 'request-top');
  const label = element('div');
  label.append(
    element('p', 'request-kind', requestKind(request)),
    element('p', 'request-id', 'Запрос #' + String(request.id ?? '—')),
  );
  top.append(label, element('span', 'request-status status-' + String(request.status ?? ''), statusNames[request.status] ?? request.status ?? 'неизвестно'));
  card.append(top);

  if (request.text && kind !== 'invariant_output') card.append(element('p', 'request-text', request.text));
  if (kind === 'invariant_output') card.append(element('p', 'request-text', 'Проверен подготовленный ответ; его сырой текст в журнале не показывается.'));
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
  const snapshot = invariantSnapshot(request.metadata);
  if (snapshot) {
    card.append(element('p', 'invariant-ledger-line', 'Снимок правил: задача #' + String(snapshot.task_id ?? '—')
      + ' · ревизия ' + String(snapshot.revision ?? '—') + ' · ' + integer(snapshot.rules.length) + ' шт.'));
  }
  const verdict = invariantVerdictText(request.metadata?.invariant_check);
  if (verdict) card.append(element('p', 'invariant-ledger-line', verdict));
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

function renderKnowledge(records) {
  const list = Array.isArray(records) ? records : [];
  dom.knowledgeCount.textContent = String(list.length);
  const fragment = document.createDocumentFragment();
  if (!list.length) fragment.append(element('p', 'empty-copy', 'Поиск ещё не выполнялся. Выберите источники перед отправкой вопроса.'));
  for (const record of [...list].reverse()) {
    const batch = element('article', 'knowledge-batch');
    const provider = record.provider === 'wikipedia' ? 'Wikipedia' : 'Stack Exchange';
    batch.append(element('h3', '', provider + ' · запрос #' + record.request_id + ' · ' + record.query));
    if (record.status === 'error') batch.append(element('p', 'notice error', record.error || 'Источник недоступен. Основной ответ не сгенерирован.'));
    else if (!record.sources?.length) batch.append(element('p', 'empty-copy', 'Материалы не найдены. Попробуйте другую короткую тему.'));
    for (const source of record.sources ?? []) {
      const card = element('div', 'knowledge-card');
      const link = element('a', '', source.title || 'Открыть источник');
      try {
        const url = new URL(source.url);
        if (url.protocol === 'https:') { link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; }
      } catch { /* A malformed URL remains plain text. */ }
      card.append(link);
      const meta = [source.author, source.date, source.score !== undefined ? 'Рейтинг: ' + source.score : null].filter(Boolean);
      if (meta.length) card.append(element('p', 'field-note', meta.join(' · ')));
      const detail = element('details');
      detail.append(element('summary', '', 'Прочитать полученный фрагмент'), element('p', '', source.excerpt || 'Текст отсутствует.'));
      card.append(detail);
      batch.append(card);
    }
    fragment.append(batch);
  }
  dom.knowledgeResults.replaceChildren(fragment);
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
  renderPersonalization(state, {force: options.forceDrafts});
  renderWorkspace(state);
  renderTaskState(state);
  renderInvariants(state, {force: options.forceDrafts});
  renderConversation(state.messages);
  renderKnowledge(state.retrievals);
  renderMemory(state);
  renderAccounting(state);
  renderRequests(state.requests);
  renderBranches(state);
  dom.bootStatus.textContent = state.boot_id ? 'Запуск ' + String(state.boot_id).slice(0, 8) : 'Состояние сохранено';
  dom.sessionInfo.textContent = 'Диалог #' + String(activeWorkspace(state)?.id ?? '—') + ' · профиль ' + String(state.personalization?.profile?.name ?? state.workspace.user_id ?? 'local');
  if (options.scroll) requestAnimationFrame(() => dom.conversation.scrollTo({top: dom.conversation.scrollHeight, behavior: 'smooth'}));
  syncControls();
  document.dispatchEvent(new CustomEvent('workspace-state', {detail: state}));
}

async function loadState(options = {}) {
  if (busy) return;
  ready = false;
  setBusy(true, 'Загрузка');
  setNotice('');
  try {
    const {response, body} = await fetchJson('/api/state', {cache: 'no-store'});
    if (!response.ok) throw new Error(errorText(body, 'Не удалось загрузить рабочее пространство.'));
    renderState(body, {forceDrafts: options.discardDrafts === true});
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
  window.openWorkspacePanel?.('usage');
  setBusy(true, 'Собираем состав');
  setNotice('');
  try {
    const {response, body} = await fetchJson('/api/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        ...activeRequestIdentity(),
        prompt,
        use_working: true,
        use_long_term: true,
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
  window.openWorkspacePanel?.('usage');
  setBusy(true, 'Загрузка трассы');
  setNotice('');
  try {
    const {response, body} = await fetchJson('/api/trace/' + encodeURIComponent(requestId), {cache: 'no-store'});
    if (!response.ok || body.status !== 'ok') throw new Error(errorText(body, 'Трасса недоступна.'));
    const fragment = document.createDocumentFragment();
    const snapshot = invariantSnapshot(body.metadata) ?? invariantSnapshot(body.context);
    const invariantSection = element('section', 'trace-section');
    invariantSection.append(element('h4', '', 'Правила задачи в этом вызове'));
    if (snapshot) {
      invariantSection.append(element('p', 'context-line', 'Задача #' + String(snapshot.task_id ?? '—') + ' · ревизия ' + String(snapshot.revision ?? '—')));
      invariantSection.append(...(snapshot.rules.length
        ? snapshot.rules.map((rule, index) => {
            const card = element('article', 'trace-memory-card trace-invariant-card');
            card.append(
              element('p', 'memory-key', rule.id || 'R' + String(index + 1)),
              element('p', 'memory-value', rule.text ?? ''),
            );
            return card;
          })
        : [emptyCopy('Для вызова сохранён пустой снимок правил.') ]));
    } else {
      invariantSection.append(emptyCopy('Этот вызов не содержит снимка правил задачи.'));
    }
    fragment.append(invariantSection);
    const invariantCheck = body.metadata?.invariant_check;
    if (invariantCheck && Array.isArray(invariantCheck.checks)) {
      const checkSection = element('section', 'trace-section');
      checkSection.append(element('h4', '', 'Вердикт смысловой проверки'));
      checkSection.append(...invariantCheck.checks.map(item => {
        const card = element('article', 'trace-memory-card invariant-verdict-card ' + (item.violated === true ? 'violated' : 'passed'));
        card.append(
          element('p', 'memory-key', String(item.id ?? '—')),
          element('p', 'memory-value', item.violated === true ? 'Нарушено' : item.violated === false ? 'Соблюдено' : 'Нет корректного вердикта'),
        );
        return card;
      }));
      fragment.append(checkSection);
    }
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
    const safeMetadata = safeTraceMetadata(body.metadata ?? {});
    delete safeMetadata.invariants;
    delete safeMetadata.invariant_check;
    metadata.append(element('h4', '', 'Остальные метаданные вызова'), element('pre', 'trace-json', jsonText(safeMetadata)));
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
  const sourceChoice = dom.knowledgeSources.value;
  const sources = sourceChoice === 'off' ? [] : sourceChoice === 'both' ? ['wikipedia', 'stackexchange'] : [sourceChoice];
  if (sources.length && !dom.knowledgeQuery.value.trim()) {
    window.openWorkspacePanel?.('sources');
    setNotice('Введите короткую тему поиска в панели источников.', 'error');
    dom.knowledgeQuery.focus();
    return;
  }
  const body = await mutation('/api/ask', {
    ...activeRequestIdentity(),
    prompt,
    use_working: true,
    use_long_term: true,
    retrieval: {sources, query: dom.knowledgeQuery.value.trim(), wikipedia_query: dom.knowledgeWikiQuery.value.trim(), language: dom.knowledgeLanguage.value, community: dom.knowledgeCommunity.value},
  }, 'Агент работает', '', {scroll: true});
  if (body?.status === 'ok') {
    dom.prompt.value = '';
    dom.previewResult.hidden = true;
    dom.previewResult.textContent = '';
    updatePromptCount();
    setNotice('Ответ готов. Автоматическое распределение и все расходы сохранены.', 'success');
  }
});

dom.invariantForm.addEventListener('submit', async event => {
  event.preventDefault();
  const draft = updateInvariantRuleHelp();
  const identity = activeRequestIdentity();
  const revision = Number(currentState?.invariants?.revision);
  if (!draft.valid) {
    setNotice(draft.error, 'error');
    return;
  }
  if (!Number.isFinite(identity.task_id) || !Number.isFinite(identity.profile_id) || !Number.isFinite(revision)) {
    setNotice('Не удалось определить задачу, профиль или ревизию правил. Обновите состояние.', 'error');
    return;
  }
  await mutation('/api/invariants', {
    task_id: identity.task_id,
    profile_id: identity.profile_id,
    revision,
    rules: draft.rules,
  }, 'Сохраняем правила', draft.rules.length
    ? 'Правила сохранены и будут проверяться в следующем запросе.'
    : 'Все дополнительные правила удалены явным сохранением.', {
    method: 'PUT',
    afterSuccess: body => {
      setInvariantDirty(false);
      if (body.state) renderInvariants(body.state, {force: true});
    },
  });
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
dom.knowledgeSources.addEventListener('change', syncControls);
dom.preview.addEventListener('click', showPreview);
dom.refresh.addEventListener('click', () => {
  if (!confirmDraftLoss('Обновление состояния')) return;
  loadState({discardDrafts: true});
});

dom.profileSelect.addEventListener('change', async () => {
  const profileId = Number(dom.profileSelect.value);
  const previousId = selectedProfileId();
  if (!profileId || String(profileId) === String(previousId)) return;
  if (!confirmDraftLoss('Переключение профиля')) {
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
  if (!name || !confirmDraftLoss('Создание нового профиля')) return;
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
    if (!example || !confirmDraftLoss('Создание демонстрационного профиля')) return;
    const payload = {...example, name: availableProfileName(example.name)};
    knownMessageIds = new Set();
    await mutation('/api/profiles', payload, 'Создаём демо-профиль', 'Демонстрационный профиль «' + payload.name + '» создан и выбран.', {scroll: true});
  });
});

dom.openDialogue.addEventListener('click', () => {
  if (!dom.dialogueSelect.value) return;
  const target = (currentState?.workspace?.dialogues ?? []).find(item => String(item.id) === String(dom.dialogueSelect.value));
  const switchesTask = target && String(target.task_id) !== String(activeWorkspace()?.task_id);
  if (switchesTask && !confirmDraftLoss('Открытие другой задачи', {profile: false})) {
    dom.dialogueSelect.value = String(activeWorkspace()?.id ?? '');
    syncControls();
    return;
  }
  knownMessageIds = new Set();
  mutation('/api/open', {dialogue_id: Number(dom.dialogueSelect.value)}, 'Открываем диалог', 'Диалог и его краткосрочная история загружены.', {scroll: true});
});

dom.dialogueList.addEventListener('click', event => {
  const button = event.target.closest('[data-dialogue-id]');
  if (!button || button.disabled || busy || !ready) return;
  dom.dialogueSelect.value = button.dataset.dialogueId;
  syncControls();
  dom.openDialogue.click();
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
  if (!confirmDraftLoss('Создание новой задачи', {profile: false})) return;
  knownMessageIds = new Set();
  mutation('/api/task', {name, project, mode: dom.taskMode.value}, 'Создаём задачу', 'Новая задача и пустой диалог открыты.', {
    afterSuccess: () => {
      dom.taskName.value = '';
      dom.taskProject.value = '';
      $('#task-create-details').open = false;
    },
  });
});

document.addEventListener('input', event => {
  if (event.target.matches('#profile-style, #profile-constraints')) {
    const saved = profileValues(currentState?.personalization?.profile);
    const draft = profileDraft();
    setProfileDirty(draft.style !== saved.style || draft.format !== saved.format || draft.constraints !== saved.constraints);
  }
  if (event.target.matches('#invariant-rules')) {
    updateInvariantDirty();
  }
  if (event.target.matches('#profile-name, #dialogue-name, #task-name, #task-project, #checkpoint-name, #branch-name')) syncControls();
});
dom.profileFormat.addEventListener('change', () => {
  const saved = profileValues(currentState?.personalization?.profile);
  const draft = profileDraft();
  setProfileDirty(draft.style !== saved.style || draft.format !== saved.format || draft.constraints !== saved.constraints);
});
window.addEventListener('beforeunload', event => {
  if (!profileDirty && !invariantDirty) return;
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
  if (action !== 'delete-memory') return;
  const memoryId = Number(button.dataset.memoryId);
  const longTerm = currentState?.workspace?.layers?.long_term;
  const entry = Array.isArray(longTerm) ? longTerm.find(item => Number(item.id) === memoryId) : null;
  if (!entry || !isReferenceMemory(entry)) return;
  mutation('/api/memory/' + memoryId, null, 'Забываем запись', 'Запись забыта в будущих ответах; её версии сохранены для трассировки.', {method: 'DELETE'});
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

updatePromptCount();
loadState();
