import ast
from pathlib import Path


def test_routers_do_not_import_database_or_process_modules() -> None:
    root = Path(__file__).parents[1] / "src" / "heimdall"
    forbidden = {"psycopg", "subprocess", "heimdall.database", "heimdall.runtime"}
    violations: list[str] = []

    for path in root.rglob("router.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == item or name.startswith(f"{item}.") for item in forbidden):
                    violations.append(f"{path.relative_to(root)} imports {name}")

    assert violations == []
