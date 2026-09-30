"""Optional browser regressions against a real local Flask API and isolated data."""

import os
from threading import Thread
from weakref import WeakKeyDictionary

import pytest
from werkzeug.serving import make_server

from app import create_app
from test_automatic_mcp import Sources, tool_reply
from test_strategies import Fake, response


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_UI_TESTS") != "1",
    reason="Set RUN_UI_TESTS=1 to run the optional Playwright browser suite",
)
PAGE_ERRORS = WeakKeyDictionary()


@pytest.fixture
def browser_app(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    fake = Fake([response("Проверьте направление тангентов."), response('{"operations": []}')])
    app = create_app(data_dir=tmp_path, transport=fake)
    workspace = app.extensions["workspace"]
    task_id = workspace.memory.workspace()["active_dialogue"]["task_id"]
    workspace.memory.save(task_id, dict(layer="working", category="context", scope="task",
                                        key="Рабочий маркер", value="Только текущая задача", reason="Тест"))
    workspace.memory.save(task_id, dict(layer="long_term", category="knowledge", scope="user",
                                        key="Справочный маркер", value="Рисую в Blender", reason="Тест"))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                yield f"http://127.0.0.1:{server.server_port}", browser, fake, workspace
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        workspace.close()


def open_page(browser, url, *, width=1360, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    PAGE_ERRORS[page] = errors
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    page.locator("#boot-status").get_by_text("Загрузка").wait_for(state="hidden")
    return page


def close_page(page):
    errors = PAGE_ERRORS.pop(page, [])
    page.close()
    assert not errors, f"Browser JavaScript errors: {errors}"


def open_panel(page, name):
    trigger = page.locator(f'[data-panel="{name}"]').first
    trigger.click()
    page.locator("#workspace-dialog").wait_for(state="visible")
    assert page.locator(f'[data-workspace-panel="{name}"]').is_visible()
    return trigger


def close_panel(page):
    page.locator("#workspace-dialog [data-close-panel]").click()
    page.locator("#workspace-dialog").wait_for(state="hidden")


def test_panels_keyboard_focus_and_mobile_navigation(browser_app):
    url, browser, _, _ = browser_app
    page = open_page(browser, url)
    try:
        for name in ("profile", "task", "rules", "sources", "usage", "branches", "create"):
            trigger = open_panel(page, name)
            assert page.evaluate("document.activeElement.matches('#workspace-dialog [data-close-panel]')")
            page.keyboard.press("Escape")
            page.locator("#workspace-dialog").wait_for(state="hidden")
            assert trigger.evaluate("el => document.activeElement === el")

        held_preview = []
        page.route("**/api/preview", lambda route: held_preview.append(route))
        page.locator("#prompt").fill("Как устроена нормаль?")
        with page.expect_request("**/api/preview"):
            page.locator("#preview-button").click()
        page.locator("#workspace-dialog").wait_for(state="visible")
        page.wait_for_function("document.querySelector('#preview-button').disabled")
        page.keyboard.press("Escape")
        page.locator("#workspace-dialog").wait_for(state="hidden")
        assert page.locator('.hud [data-panel="usage"]').evaluate(
            "el => document.activeElement === el"
        )
        assert len(held_preview) == 1
        held_preview[0].abort()
        page.wait_for_function("!document.querySelector('#prompt').disabled")
    finally:
        close_page(page)

    mobile = open_page(browser, url, width=390, height=844)
    try:
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        mobile.locator('[data-shell-toggle="chats"]').click()
        assert mobile.locator("#chat-sidebar").is_visible()
        assert mobile.locator("#dialogue-list .dialogue-item").count() >= 1
        open_panel(mobile, "profile")
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        close_panel(mobile)
        mobile.locator('[data-shell-toggle="context"]').first.click()
        assert mobile.locator("#context-sidebar").is_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        close_page(mobile)


def test_profile_save_switch_and_dirty_guard(browser_app):
    url, browser, _, workspace = browser_app
    page = open_page(browser, url)
    try:
        open_panel(page, "profile")
        page.locator("#profile-style").fill("Объясняй коротко")
        page.locator("#profile-format").select_option("steps")
        page.locator("#profile-constraints").fill("Только Blender")
        page.locator("#save-profile").click()
        page.wait_for_function(
            "document.querySelector('#profile-summary').textContent.includes('Объясняй коротко')"
        )
        saved = workspace.state()["personalization"]["profile"]
        assert (saved["style"], saved["format"], saved["constraints"]) == (
            "Объясняй коротко", "steps", "Только Blender")
        assert "Справочный маркер" in page.locator("#memory-long").inner_text()
        assert "Рабочий маркер" not in page.locator("#memory-long").inner_text()
        assert "personalization." not in page.locator("#memory-long").inner_text()

        page.locator(".profile-create").evaluate("el => el.open = true")
        page.locator("#profile-name").fill("Второй")
        page.locator("#create-profile").click()
        page.locator("#profile-select").wait_for(state="visible")
        page.wait_for_function("document.querySelector('#profile-select').options.length === 2")
        second_id = workspace.state()["personalization"]["selected_id"]
        assert workspace.state()["personalization"]["profile"]["style"] == ""
        page.locator("#profile-style").fill("Несохранённый текст")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#profile-select").select_option(str(saved["id"]))
        assert workspace.state()["personalization"]["selected_id"] == second_id
        assert page.locator("#profile-style").input_value() == "Несохранённый текст"
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#profile-select").select_option(str(saved["id"]))
        page.wait_for_function(
            "document.querySelector('#profile-style').value === 'Объясняй коротко'"
        )
        assert workspace.state()["personalization"]["selected_id"] == saved["id"]
        assert page.locator("#profile-style").input_value() == "Объясняй коротко"
    finally:
        close_page(page)


def test_chat_api_memory_sources_usage_pause_and_branches(browser_app):
    url, browser, fake, workspace = browser_app
    page = open_page(browser, url)
    requests = []
    page.on("request", lambda req: requests.append(req) if req.url.endswith("/api/ask") else None)
    try:
        page.locator("#prompt").fill("Почему швы видны?")
        page.locator("#preview-button").click()
        page.locator("#token-preview").wait_for(state="visible")
        close_panel(page)
        open_panel(page, "sources")
        assert "сам решает" in page.locator('[data-workspace-panel="sources"]').inner_text()
        assert "В этом диалоге поиск не выполнялся" in page.locator("#knowledge-results").inner_text()
        assert page.locator("#knowledge-sources, #knowledge-query, #knowledge-wiki-query, #knowledge-language, #knowledge-community").count() == 0
        close_panel(page)
        page.locator("#send").click()
        page.locator("#conversation .message.assistant").last.wait_for()
        assert requests
        body = requests[-1].post_data_json
        assert body["use_working"] is body["use_long_term"] is True
        assert "retrieval" not in body
        assert fake.calls
        assert page.locator("#source-launch").inner_text() == "Источники ответа"
        assert "В этом диалоге поиск не выполнялся" in page.locator("#knowledge-results").inner_text()
        page.locator("#conversation [data-action='trace']").last.click()
        page.locator("#trace-content").get_by_text("Рабочий маркер").wait_for()
        assert page.locator("#chat-calls").inner_text() != "0"
        close_panel(page)

        open_panel(page, "profile")
        assert page.locator("#memory-long").get_by_text("Справочный маркер").count() == 1
        page.locator("#memory-long [data-action='delete-memory']").click()
        page.locator("#memory-long").get_by_text("Справочный маркер").wait_for(state="hidden")
        assert workspace.state()["workspace"]["layers"]["working"][0]["key"] == "Рабочий маркер"
        close_panel(page)

        open_panel(page, "task")
        page.locator("#toggle-task-pause").click()
        page.get_by_text("На паузе").first.wait_for()
        assert page.locator("#prompt").is_disabled()
        assert page.locator("#send").is_disabled()
        page.locator("#toggle-task-pause").click()
        page.wait_for_function("!document.querySelector('#prompt').disabled")
        assert page.locator("#send").is_disabled()  # Earlier successful send cleared the prompt.
        close_panel(page)
        page.locator("#prompt").fill("Следующий вопрос")
        assert not page.locator("#send").is_disabled()
        page.locator("#prompt").fill("")

        open_panel(page, "create")
        page.locator("#dialogue-name").fill("Ветка света")
        page.locator("#dialogue-mode").select_option("branching")
        page.locator("#create-dialogue").click()
        page.locator("#conversation-title").get_by_text("Ветка света").wait_for()
        assert workspace.state()["workspace"]["layers"]["working"][0]["key"] == "Рабочий маркер"
        close_panel(page)
        open_panel(page, "branches")
        page.locator("#checkpoint-name").fill("До контрового света")
        page.locator("#create-checkpoint").click()
        page.wait_for_function("document.querySelector('#checkpoint-select').options.length > 0")
        page.locator("#branch-name").fill("Холодный свет")
        page.locator("#create-branch").click()
        page.locator("#active-branch").get_by_text("Холодный свет").wait_for()
        page.locator("#branch-select").select_option(index=0)
        page.locator("#switch-branch").click()
        assert workspace.state()["workspace"]["active_dialogue"]["mode"] == "branching"
        close_panel(page)
        page.locator(".new-chat-button").click()
        page.locator("#task-create-details").evaluate("el => el.open = true")
        page.locator("#task-name").fill("Новая сцена")
        page.locator("#create-task").click()
        page.locator("#context-task-name").get_by_text("Новая сцена").wait_for()
        assert workspace.state()["workspace"]["layers"]["working"] == []
    finally:
        close_page(page)


def test_model_selected_sources_empty_and_error_remain_visible(browser_app):
    url, browser, fake, workspace = browser_app
    sources = Sources()
    workspace.agent()._retrieval_client = sources
    page = open_page(browser, url)
    try:
        fake.replies[:] = [
            tool_reply("lookup_wikipedia", {"query": "normal mapping", "language": "en", "limit": 2}),
            response("Normal maps change the lighting direction."),
            response('{"operations": []}'),
        ]
        page.locator("#prompt").fill("Что такое normal mapping?")
        page.locator("#send").click()
        page.locator("#conversation .message.assistant").last.wait_for()
        assert sources.calls[0][:2] == ("wikipedia", "normal mapping")
        open_panel(page, "sources")
        page.locator("#knowledge-results").get_by_role("link", name="Example reference").wait_for()
        assert "normal mapping" in page.locator("#knowledge-results").inner_text()
        close_panel(page)
        open_panel(page, "usage")
        assert "Шаг с источниками" in page.locator("#request-list").inner_text()
        assert page.locator("#chat-cost").inner_text() != "—"
        close_panel(page)

        sources.empty = True
        fake.replies[:] = [
            tool_reply("lookup_wikipedia", {"query": "missing technique", "language": "en", "limit": 2}),
            response("I found no source for that technique."),
            response('{"operations": []}'),
        ]
        page.locator("#prompt").fill("Найди missing technique")
        page.locator("#send").click()
        page.wait_for_function("document.querySelector('#knowledge-count').textContent === '2'")
        open_panel(page, "sources")
        page.locator("#knowledge-results").get_by_text("Поиск выполнен, но материалы не найдены.").wait_for()
        close_panel(page)

        sources.fail = "wikipedia"
        fake.replies[:] = [
            tool_reply("lookup_wikipedia", {"query": "unavailable topic", "language": "en", "limit": 2}),
        ]
        page.locator("#prompt").fill("Найди unavailable topic")
        page.locator("#send").click()
        page.wait_for_function("document.querySelector('#knowledge-count').textContent === '3'")
        open_panel(page, "sources")
        page.locator("#knowledge-results .notice.error").wait_for()
        close_panel(page)
        page.reload()
        page.locator("#boot-status").get_by_text("Загрузка").wait_for(state="hidden")
        open_panel(page, "sources")
        assert page.locator("#knowledge-results").get_by_role("link", name="Example reference").count() == 1
        assert "Поиск выполнен, но материалы не найдены." in page.locator("#knowledge-results").inner_text()
        assert page.locator("#knowledge-results .notice.error").is_visible()
    finally:
        close_page(page)


def test_rule_draft_survives_panel_close_and_guards_task_switch(browser_app):
    url, browser, _, workspace = browser_app
    page = open_page(browser, url)
    try:
        initial = workspace.state()["workspace"]
        first_dialogue = initial["active_dialogue"]["id"]
        first_task_name = next(task["name"] for task in initial["tasks"]
                               if task["id"] == initial["active_dialogue"]["task_id"])
        open_panel(page, "rules")
        page.locator("#invariant-rules").fill("Только Blender")
        page.locator("#save-invariants").click()
        page.locator("#invariant-active-list").get_by_text("Только Blender").wait_for()
        close_panel(page)

        page.locator(".new-chat-button").click()
        page.locator("#task-create-details summary").click()
        page.locator("#task-name").fill("Вторая задача")
        page.locator("#create-task").click()
        page.locator("#context-task-name").get_by_text("Вторая задача").wait_for()
        second_dialogue = workspace.state()["workspace"]["active_dialogue"]["id"]
        close_panel(page)
        page.locator(f'#dialogue-list [data-dialogue-id="{first_dialogue}"]').click()
        page.locator("#context-task-name").get_by_text(first_task_name).wait_for()

        open_panel(page, "rules")
        assert "Только Blender" in page.locator("#invariant-active-list").inner_text()
        page.locator("#invariant-rules").fill("Только Cycles")
        close_panel(page)
        open_panel(page, "rules")
        assert page.locator("#invariant-rules").input_value() == "Только Cycles"
        close_panel(page)

        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator(f'#dialogue-list [data-dialogue-id="{second_dialogue}"]').click()
        assert workspace.state()["workspace"]["active_dialogue"]["id"] == first_dialogue
        open_panel(page, "rules")
        assert page.locator("#invariant-rules").input_value() == "Только Cycles"
        close_panel(page)

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator(f'#dialogue-list [data-dialogue-id="{second_dialogue}"]').click()
        page.locator("#context-task-name").get_by_text("Вторая задача").wait_for()
        open_panel(page, "rules")
        assert page.locator("#invariant-rules").input_value() == ""
        assert "Только Blender" not in page.locator("#invariant-active-list").inner_text()
        close_panel(page)

        open_panel(page, "profile")
        page.locator(".profile-create summary").click()
        page.locator("#profile-name").fill("Основной")
        with page.expect_response(lambda response: response.url.endswith("/api/profiles") and response.status == 400):
            page.locator("#create-profile").click()
        page.locator("#panel-notice").get_by_text("уже существует").wait_for()
        assert page.locator("#workspace-dialog").is_visible()
        assert workspace.state()["personalization"]["profile"]["name"] == "Основной"
    finally:
        close_page(page)
