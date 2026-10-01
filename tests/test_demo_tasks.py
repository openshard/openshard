from __future__ import annotations

import json
from pathlib import Path

DEMO_TASKS_PATH = Path(__file__).parent.parent / "examples" / "demo-tasks.json"
DEMO_INFRA_DIR = Path(__file__).parent.parent / "examples" / "production-infra-demo"

REQUIRED_FIELDS = {"name", "category", "where", "input", "expected", "safety_note"}

REQUIRED_CATEGORIES = {
    "production_iac_hardening",
    "terraform_networking_review",
    "iam_security_review",
    "cicd_pipeline_review",
}

EXPECTED_DEMO_FILES = [
    ".gitignore",
    "README.md",
    "demo-task.md",
    "main.tf",
    "variables.tf",
    "network.tf",
    "database.tf",
    "iam.tf",
    "services.tf",
    "storage.tf",
    "secrets.tf",
    "monitoring.tf",
    "outputs.tf",
    "terraform.tfvars.example",
]

FORBIDDEN_STRINGS = ["PRIVATE_COMPANY", "INTERNAL_CUSTOMER", "AKIA", "sk-live", "sk-prod"]


def _load():
    return json.loads(DEMO_TASKS_PATH.read_text(encoding="utf-8"))


def test_demo_tasks_file_exists():
    assert DEMO_TASKS_PATH.exists()


def test_demo_tasks_valid_json():
    _load()


def test_demo_tasks_is_nonempty_list():
    data = _load()
    assert isinstance(data, list) and len(data) >= 6


def test_each_task_has_required_fields():
    for i, task in enumerate(_load()):
        missing = REQUIRED_FIELDS - set(task)
        assert not missing, f"Task {i} ({task.get('name', '?')!r}) missing: {missing}"


def test_each_task_fields_are_strings():
    for i, task in enumerate(_load()):
        for field in REQUIRED_FIELDS:
            assert isinstance(task.get(field), str), (
                f"Task {i} ({task.get('name', '?')!r}) field {field!r} must be a string"
            )


def test_required_categories_present():
    categories = {t["category"] for t in _load() if "category" in t}
    missing = REQUIRED_CATEGORIES - categories
    assert not missing, f"demo-tasks.json missing required categories: {missing}"


def test_production_infra_demo_files_exist():
    for filename in EXPECTED_DEMO_FILES:
        path = DEMO_INFRA_DIR / filename
        assert path.exists(), f"Missing examples/production-infra-demo/{filename}"


def test_terraform_tfvars_not_present():
    tfvars = DEMO_INFRA_DIR / "terraform.tfvars"
    assert not tfvars.exists(), "terraform.tfvars must not be committed"


def test_terraform_tfvars_example_present():
    example = DEMO_INFRA_DIR / "terraform.tfvars.example"
    assert example.exists()


def test_demo_files_no_forbidden_strings():
    for path in DEMO_INFRA_DIR.iterdir():
        if path.is_file():
            content = path.read_text(encoding="utf-8", errors="ignore")
            for word in FORBIDDEN_STRINGS:
                assert word not in content, (
                    f"{path.name} contains forbidden string: {word!r}"
                )