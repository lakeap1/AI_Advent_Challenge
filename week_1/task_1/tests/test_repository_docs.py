from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_every_task_has_readme():
    missing = [
        str(Path(f"week_{week}") / f"task_{task}" / "README.md")
        for week in range(1, 8)
        for task in range(1, 6)
        if not (
            REPOSITORY_ROOT / f"week_{week}" / f"task_{task}" / "README.md"
        ).is_file()
    ]
    assert missing == []


def test_repository_has_root_readme():
    assert (REPOSITORY_ROOT / "README.md").is_file()


def test_unstarted_tasks_do_not_claim_completion():
    implemented_tasks = {(1, 1), (1, 2), (1, 3), (1, 4), (1, 5)}
    for week in range(1, 8):
        for task in range(1, 6):
            if (week, task) in implemented_tasks:
                continue

            content = (
                REPOSITORY_ROOT / f"week_{week}" / f"task_{task}" / "README.md"
            ).read_text(encoding="utf-8")
            assert "Условие ещё не получено" in content
            assert "Статус: выполнено" not in content


def test_readmes_do_not_contain_patch_markers():
    readmes = [REPOSITORY_ROOT / "README.md"]
    readmes.extend(
        REPOSITORY_ROOT / f"week_{week}" / f"task_{task}" / "README.md"
        for week in range(1, 8)
        for task in range(1, 6)
    )

    malformed = [
        str(path.relative_to(REPOSITORY_ROOT))
        for path in readmes
        if any(line.startswith("+") for line in path.read_text(encoding="utf-8").splitlines())
    ]

    assert malformed == []
