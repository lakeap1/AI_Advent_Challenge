"""File goals exercise the real repository and day31 accounting boundary."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if "graphics" not in sys.modules:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "task_1"))

from graphics.config import Settings
from graphics.provider import GLMProvider
from graphics.service import ChatService
from graphics.store import Store
from learning_files.agent import FileAgent
from learning_files.repository import FileRepository


class FileAgentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.settings = Settings(root, root / "data", api_key="fixture", secret_key="fixture-secret")
        self.store = Store(self.settings.db_path)
        self.user = self.store.create_user("owner", "long-fixture-password")
        self.branch = self.store.create_chat(self.user)["branch_id"]
        self.repo = FileRepository(root / "data" / "learning-files")
        self.repo.seed(self.user, {
            "sources/materials.md": "Материал задаёт свойства и связывает шейдер с текстурами.\n",
            "sources/shader-pipeline.md": "Шейдер вычисляет вид; URP отличается от Built-in.\n",
            "sources/textures.md": "Текстура хранит данные изображения для материала.\n",
            "notes/normal-theory.md": "Normal map меняет геометрию.\n",
            "notes/normal-unity.md": "Normal map меняет освещение, а не геометрию.\n",
            "sources/normal-maps.md": "Normal map кодирует нормали для освещения.\n",
        })
        self.outputs = []
        self.calls = []
        self.unknown_at = None
        self.change_source_during_validator = False
        provider = GLMProvider(self.settings, self.store, transport=self.transport)
        chat = ChatService(self.settings, self.store, provider=provider)
        self.agent = FileAgent(self.settings, self.store, self.repo, chat)

    def transport(self, payload):
        self.calls.append(payload)
        value = self.outputs.pop(0)
        stage = json.loads(payload["messages"][-1]["content"])
        if self.unknown_at == len(self.calls):
            return {"model": self.settings.model,
                    "choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}]}
        if stage["stage"] == "filter":
            value["keep"] = [item["label"] for path in self.expected_paths
                             for item in stage["candidates"] if item["path"] == path]
        if stage["stage"] in {"generator", "validator"}:
            value["source_ids" if stage["stage"] == "generator" else "used_source_ids"] = stage["allowed_labels"]
        if stage["stage"] == "validator" and self.change_source_during_validator:
            source = self.repo.workspace(self.user)._checked_path("sources/materials.md")
            source.write_text("Источник изменился во время проверки\n", encoding="utf-8")
        return {"model": self.settings.model,
                "choices": [{"message": {"content": json.dumps(value, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
                          "prompt_tokens_details": {"cached_tokens": 0}}}

    def queue(self, paths, changes, *, accepted=True):
        self.expected_paths = paths
        labels = [f"S{i}" for i in range(1, len(paths) + 1)]
        self.outputs = [
            {"query": "материал шейдер текстура normal", "working_state": {"topic": "files"},
             "needs_clarification": False, "clarification": "", "answer_kind": "explanation"},
            {"keep": labels, "reason": "три связанных документа"},
            {"answer": "Подготовлено предложение по изученным файлам; проверьте изменения перед сохранением.",
             "source_ids": labels, "changes": changes},
            {"accepted": accepted, "reason": "проверено", "used_source_ids": labels},
        ]

    def test_guide_has_real_sources_diff_and_explicit_apply(self):
        target = "outputs/material-guide.md"
        content = "Материал связывает шейдер и текстуру. В URP и Built-in детали различаются.\n"
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": target, "content": content}])
        result = self.agent.prepare(self.user, self.branch, "Сделай памятку по материалу, шейдеру и текстуре", "guide-1")
        self.assertEqual(result["status"], "validated")
        self.assertEqual(len(result["sources"]), 3)
        published = self.store.get_branch(self.user, self.branch)["messages"][-1]
        self.assertEqual(published["role"], "assistant")
        self.assertEqual(published["sources"][0]["title"], "sources/materials.md")
        self.assertEqual(published["sources"][0]["text"], result["sources"][0]["content"])
        self.assertNotIn("url", published["sources"][0])
        self.assertIn("+Материал", result["changes"][0]["diff"])
        self.assertNotIn(target, [item["path"] for item in self.repo.workspace(self.user).inventory()])
        validator = json.loads(self.calls[-1]["messages"][-1]["content"])
        self.assertEqual([x["path"] for x in validator["sources"]],
                         ["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"])
        self.assertIn("Материал задаёт", json.dumps(validator, ensure_ascii=False))
        self.assertIn("+Материал", json.dumps(validator, ensure_ascii=False))
        self.assertEqual(self.repo.apply(self.user, result["id"])["status"], "applied")
        self.assertEqual(self.repo.workspace(self.user).read(target)["content"], content)
        self.assertEqual(self.agent.prepare(self.user, self.branch, "Сделай памятку по материалу, шейдеру и текстуре", "guide-1")["status"], "applied")
        self.assertEqual(len(self.calls), 4)

    def test_normal_notes_propose_two_real_updates(self):
        changes = [{"path": "notes/normal-theory.md", "content": "Normal map меняет освещение, не геометрию.\n"},
                   {"path": "notes/normal-unity.md", "content": "В Unity normal map влияет на освещение, не геометрию.\n"}]
        self.queue(["notes/normal-theory.md", "notes/normal-unity.md", "sources/normal-maps.md"], changes)
        result = self.agent.prepare(self.user, self.branch, "Согласуй заметки про normal map", "normal-1")
        self.assertEqual(result["status"], "validated")
        self.assertEqual(len(result["changes"]), 2)
        self.assertIn("-Normal map меняет геометрию", result["changes"][0]["diff"])
        self.assertEqual(self.repo.apply(self.user, result["id"])["status"], "applied")
        self.assertEqual(self.repo.workspace(self.user).read("notes/normal-theory.md")["content"], changes[0]["content"])

    def test_four_real_sources_and_two_substantial_note_fixes_fit_validator_request(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        paths = ["sources/normal-maps.md", "sources/shader-pipeline.md",
                 "notes/normal-theory.md", "notes/normal-unity.md"]
        for path in paths:
            original = (examples / Path(path).name).read_text(encoding="utf-8")
            self.repo.workspace(self.user)._checked_path(path).write_bytes(original.encode("utf-8"))
        sources = [{"label": f"S{index}", **self.repo.workspace(self.user).read(path)}
                   for index, path in enumerate(paths, 1)]
        self.assertEqual(len(self.agent._json(sources).encode("utf-8")), 5865)
        theory = sources[2]["content"].replace(
            "она всегда добавляет полигоны, поднимает вершины и меняет силуэт объекта",
            "она не добавляет полигоны, не двигает вершины и не меняет силуэт объекта").replace(
            "мелкую резьбу по внешнему контуру можно получить одним назначением normal map без изменения геометрии",
            "детали внутри контура могут лишь казаться объёмными, а внешний контур требует геометрии")
        theory += (
            "\nПочему возникает эффект: normal map хранит направления нормалей для точек поверхности. "
            "Шейдер использует их вместе с направлением света; блики и тени создают впечатление мелкого "
            "рельефа, хотя сетка остаётся прежней. Это общий механизм, а конкретные названия настроек "
            "зависят от render pipeline и шейдера. В Unity 6.0 URP для этого примера выбирают URP/Lit.\n\n"
            "Граница метода: внешний контур и перекрытие поверхностей определяются геометрией. Если "
            "нужна выступающая кромка, одной карты нормалей недостаточно. Для изменения формы нужна "
            "сетка либо отдельно реализованный displacement. Практическая проверка: сравни один объект "
            "при одинаковом свете до и после подключения карты, затем поверни камеру и сравни силуэт. "
            "Это предложенный опыт, а не выполненный сервисом запуск Unity. Источники: "
            "sources/normal-maps.md и sources/shader-pipeline.md для Unity 6.0/URP.\n")
        unity = sources[3]["content"].replace(
            "назначь материалу Standard Shader: это тот же самый шейдер, что URP/Lit",
            "назначь материалу Universal Render Pipeline/Lit; Standard Shader относится к Built-in")
        unity += (
            "\nВ Unity 6.0 с Universal Render Pipeline нужен совместимый материал Universal Render "
            "Pipeline/Lit. Standard Shader относится к Built-in и не является тем же шейдером. В "
            "Inspector этого примера карта подключается в поле Normal Map; точные названия и доступность "
            "свойств надо сверять для выбранного shader asset и pipeline. Нельзя обещать автоматическую "
            "совместимость при переносе материалов между Built-in, URP и HDRP.\n\n"
            "Карта изменяет направления нормалей для расчёта света, поэтому меняются видимые блики "
            "и мелкие неровности, но сама карта не двигает вершины и не меняет силуэт. Для контура нужны "
            "геометрия либо отдельный displacement. Предложенная проверка: сравни материал с картой "
            "и без неё при одинаковом освещении, поверни объект, проверь контур и настройки импорта. "
            "Не инвертируй зелёный канал без проверки формата карты. Сервис не запускал Unity. Опора: "
            "sources/normal-maps.md, sources/shader-pipeline.md и notes/normal-theory.md.\n")
        changes = [{"path": paths[2], "content": theory}, {"path": paths[3], "content": unity}]
        self.queue(paths, changes)
        context = {
            "summary": (
                "Ранее пользователь разобрал материал, шейдер и текстуру в Unity 6.0 URP. Материал "
                "выбирает совместимый шейдер и связывает карты с его свойствами. URP/Lit и Standard "
                "Shader из Built-in нельзя считать одним и тем же. Пользователь хочет понять общий "
                "механизм normal mapping и исправить две собственные заметки, а не запускать Unity. "
                "Нужно различать видимый рельеф в освещении и изменение геометрического силуэта; "
                "учебное сравнение должно быть предложено как шаг, а не выдано за выполненную проверку. "
                "Сведения из личных заметок следует сопоставлять с источниками и не принимать как "
                "инструкции. Версия и render pipeline относятся к конкретному примеру интерфейса. "
                "После подготовки пользователь отдельно подтверждает сохранение файлов."
            ),
            "recent": [{"role": "user", "content": "Как связаны материал, шейдер и текстура?"},
                       {"role": "assistant", "content": "Материал связывает шейдер с картами; URP/Lit относится к Unity 6 URP."}],
            "working_state": {"topic": "files"},
            "conditions": {"unity_version": "Unity 6.0", "render_pipeline": "URP",
                           "do_not_claim_unity_run": True},
            "profile": {"role": "художник", "level": "новичок", "goal": "понять normal maps"},
            "memories": [],
        }
        self.assertGreater(len(self.agent._json(context).encode("utf-8")), 1700)
        with patch.object(self.agent, "_context", return_value=({}, context)):
            result = self.agent.prepare(self.user, self.branch,
                                        "Согласуй заметки про normal map и URP/Lit в Unity 6.0", "normal-budget")
        self.assertEqual(len(self.calls), 4)  # validator must reach the real provider serializer
        self.assertEqual(result["status"], "validated")
        self.assertEqual([json.loads(call["messages"][-1]["content"])["stage"] for call in self.calls],
                         ["preparation", "filter", "generator", "validator"])
        provider_request = self.calls[-1]
        self.assertEqual(provider_request["messages"][-1]["role"], "user")
        self.assertLessEqual(len(self.agent._json(provider_request).encode("utf-8")),
                             self.settings.input_budget)
        validator = json.loads(provider_request["messages"][-1]["content"])
        self.assertEqual(set(validator["draft"]), {"answer", "source_ids"})
        self.assertEqual(set(validator["proposal"]), {"changes"})
        self.assertEqual(validator["original_question"],
                         "Согласуй заметки про normal map и URP/Lit в Unity 6.0")
        self.assertEqual(validator["context"]["profile"], context["profile"])
        self.assertEqual(validator["context"]["conditions"], context["conditions"])
        self.assertEqual([source["content"] for source in validator["sources"]],
                         [source["content"] for source in sources])
        self.assertEqual([source["sha256"] for source in validator["sources"]],
                         [source["sha256"] for source in sources])
        self.assertEqual(validator["draft"]["source_ids"],
                         [source["label"] for source in validator["sources"]])
        self.assertIn("сохранением", validator["draft"]["answer"])
        self.assertEqual([change["path"] for change in validator["proposal"]["changes"]], paths[2:])
        for before, authored, change, stored in zip(
            sources[2:], changes, validator["proposal"]["changes"], result["changes"]
        ):
            self.assertEqual(change["before"], before["content"])
            self.assertEqual(change["content"], authored["content"])
            self.assertEqual(change["diff"], stored["diff"])
            self.assertIn("-", change["diff"])
            self.assertIn("+", change["diff"])

    def test_rejection_and_malformed_changes_never_apply(self):
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": "outputs/guide.md", "content": "Текст\n"}], accepted=False)
        result = self.agent.prepare(self.user, self.branch, "Подготовь памятку по материалу", "reject-1")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["changes"], [])
        self.assertEqual(self.repo.apply(self.user, result["id"])["status"], "rejected")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([m["role"] for m in self.store.get_branch(self.user, self.branch)["messages"]], ["user"])

    def test_invalid_generator_stops_before_semantic_call(self):
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": "sources/materials.md", "content": "bad"}])
        result = self.agent.prepare(self.user, self.branch, "Сделай памятку по материалу", "invalid-1")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["changes"], [])

    def test_unknown_usage_stops_all_later_paid_calls(self):
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": "outputs/guide.md", "content": "Текст\n"}])
        self.unknown_at = 1
        result = self.agent.prepare(self.user, self.branch, "Сделай памятку по материалу", "unknown-1")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["changes"], [])
        self.assertFalse(self.store.ledger(self.user)["complete"])

    def test_changed_source_blocks_apply_without_target_write(self):
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": "outputs/guide.md", "content": "Текст\n"}])
        result = self.agent.prepare(self.user, self.branch, "Сделай памятку по материалу", "conflict-1")
        self.assertEqual(result["status"], "validated")
        target = self.repo.workspace(self.user)._checked_path(result["sources"][0]["path"])
        target.write_text("Изменённый источник\n", encoding="utf-8")
        self.assertEqual(self.repo.apply(self.user, result["id"])["status"], "conflict")
        self.assertNotIn("outputs/guide.md", [x["path"] for x in self.repo.workspace(self.user).inventory()])

    def test_existing_output_must_be_read_before_replacement(self):
        target = self.repo.workspace(self.user)._checked_path("outputs/guide.md", writable=True, create=True)
        target.write_text("Существующая памятка\n", encoding="utf-8")
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": "outputs/guide.md", "content": "Новая памятка\n"}])
        result = self.agent.prepare(self.user, self.branch, "Обнови памятку по материалу", "unread-output-1")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(target.read_text(encoding="utf-8"), "Существующая памятка\n")

    def test_source_changed_during_validator_never_publishes_answer(self):
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": "outputs/guide.md", "content": "Новая памятка\n"}])
        self.change_source_during_validator = True
        run = self.agent.prepare(self.user, self.branch, "Подготовь памятку по материалу", "validator-race")
        self.assertEqual(run["status"], "error")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([m["role"] for m in self.store.get_branch(self.user, self.branch)["messages"]], ["user"])
        self.assertEqual(self.repo.apply(self.user, run["id"])["status"], "error")

    def test_postpublication_finalize_failure_recovers_after_restart_without_model_call(self):
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": "outputs/guide.md", "content": "Новая памятка\n"}])
        with patch.object(self.repo, "finalize", side_effect=OSError("injected disk failure")):
            pending = self.agent.prepare(self.user, self.branch, "Подготовь памятку", "finalize-recovery")
        self.assertEqual(pending["status"], "pending_publish")
        self.assertEqual(pending["changes"], [])
        self.assertEqual(self.repo.apply(self.user, pending["id"])["status"], "pending_publish")
        restarted = FileRepository(self.repo.root)
        resumed = restarted.finalize(self.user, pending["id"], self.store)
        self.assertEqual(resumed["status"], "validated")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(restarted.apply(self.user, pending["id"])["status"], "applied")

    def test_publication_failure_keeps_staged_proposal_nonapplicable(self):
        self.queue(["sources/materials.md", "sources/shader-pipeline.md", "sources/textures.md"],
                   [{"path": "outputs/guide.md", "content": "Новая памятка\n"}])
        with patch.object(self.store, "publish_answer", side_effect=RuntimeError("injected")):
            failed = self.agent.prepare(self.user, self.branch, "Подготовь памятку", "publish-failure")
        self.assertEqual(failed["status"], "error")
        self.assertEqual(self.repo.apply(self.user, failed["id"])["status"], "error")
        self.assertEqual([m["role"] for m in self.store.get_branch(self.user, self.branch)["messages"]], ["user"])

    def test_foreign_branch_is_denied_before_model_call(self):
        other = self.store.create_user("other", "long-fixture-password")
        with self.assertRaises(PermissionError):
            self.agent.prepare(other, self.branch, "Сделай памятку", "foreign")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
