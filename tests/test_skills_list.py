import json

from click.testing import CliRunner

from openshard.cli.main import cli


def test_skills_list_json_reports_local_integrity_metadata(tmp_path, monkeypatch):
    skill_dir = tmp_path / ".openshard" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: Review changes\n"
        "description: Review a proposed change\n"
        "category: review\n"
        "version: 2\n"
        "---\n"
        "Check the diff.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["skills", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == [{
        "category": "review",
        "description": "Review a proposed change",
        "digest": payload[0]["digest"],
        "name": "Review changes",
        "scope": "repository",
        "slug": "review",
        "source": "local",
        "version": "2",
    }]
    assert payload[0]["digest"].startswith("sha256:")
    assert len(payload[0]["digest"]) == 71


def test_skills_list_json_empty_is_valid_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["skills", "list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []
