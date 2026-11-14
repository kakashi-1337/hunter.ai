"""Two rules that are otherwise only prose: nothing but the runner invokes a tool handler,
and nothing but the answer route answers a permission request."""
from pathlib import Path

SRC = Path(__file__).parent.parent / "hunterai"


def files_with(needle):
    return sorted(str(f.relative_to(SRC)) for f in SRC.rglob("*.py") if needle in f.read_text())


def test_only_the_runner_calls_a_tool_handler():
    assert files_with(".handler(") == ["tools/runner.py"]


def test_only_the_answer_route_answers_a_permission_request():
    assert files_with("permissions.answer(") == ["api/runs.py"]
