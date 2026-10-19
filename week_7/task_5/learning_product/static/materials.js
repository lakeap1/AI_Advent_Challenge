"use strict";

/* App and private-files assets execute first; this extends their rendered UI. */
(() => {
  const make = (tag, className, label) => {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = label;
    return element;
  };
  const saved = new Map();
  let savedGeneration = state.authGeneration;
  const current = (generation, version, branchId, chatId) =>
    generation === state.authGeneration && version === state.navigationVersion &&
    branchId === state.branch?.id && chatId === state.chat?.id;

  function downloadLink(url, className) {
    const link = make("a", className, "Скачать");
    link.href = url;
    link.setAttribute("download", "");
    return link;
  }

  async function saveReply(message, button, actions) {
    const generation = state.authGeneration;
    const version = state.navigationVersion;
    const branchId = state.branch?.id;
    const chatId = state.chat?.id;
    if (!branchId || !chatId || state.navigating || button.disabled) return;
    button.disabled = true;
    button.textContent = "Сохраняем…";
    try {
      const result = await api(`/api/branches/${encodeURIComponent(branchId)}/materials`,
        {method: "POST", data: {message_id: message.id}});
      if (!current(generation, version, branchId, chatId)) return;
      saved.set(message.id, result.download_url);
      button.replaceWith(downloadLink(result.download_url, "download-material"));
      notice(result.created ? "Объяснение сохранено в материалах." : "Материал уже сохранён.");
    } catch (error) {
      if (!current(generation, version, branchId, chatId)) return;
      button.disabled = false;
      button.textContent = "Сохранить в материалы";
      notice(error.message, true);
    }
  }

  const originalRenderMessages = renderMessages;
  renderMessages = function (...args) {
    originalRenderMessages(...args);
    if (savedGeneration !== state.authGeneration) {
      saved.clear();
      savedGeneration = state.authGeneration;
    }
    const messages = state.branch?.messages || [];
    const articles = document.querySelectorAll("#conversation > article.message");
    for (let index = 0; index < messages.length; index++) {
      const message = messages[index];
      if (message.role !== "assistant" || message.can_save_material !== true) continue;
      const actions = articles[index]?.querySelector(".message-actions");
      if (!actions) continue;
      const url = saved.get(message.id);
      if (url) {
        actions.append(downloadLink(url, "download-material"));
      } else {
        const button = make("button", "save-material", "Сохранить в материалы");
        button.type = "button";
        button.addEventListener("click", () => saveReply(message, button, actions));
        actions.append(button);
      }
    }
  };

  const inventory = document.getElementById("files-inventory");
  if (inventory) {
    const updateDownloads = () => {
      for (const item of inventory.querySelectorAll(":scope > button")) {
        const path = item.textContent.split(" · ", 1)[0];
        if (!/^(sources|notes|outputs)\/[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.md$/.test(path)) continue;
        const next = item.nextElementSibling;
        if (next?.classList.contains("file-download") && next.dataset.path === path) continue;
        const link = downloadLink(`/api/materials/download?path=${encodeURIComponent(path)}`, "file-download");
        link.dataset.path = path;
        item.after(link);
      }
    };
    new MutationObserver(updateDownloads).observe(inventory, {childList: true});
    updateDownloads();
  }

  const intro = document.querySelector("#empty-state p");
  if (intro) intro.textContent = "Задайте вопрос о графике, уточните ответ и сохраните полезное объяснение в материалы.";
  const note = document.querySelector(".composer-note");
  if (note) note.textContent = "Источники и сохранение доступны рядом с ответом.";
})();
