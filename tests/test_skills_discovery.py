

from openshard.skills.discovery import (
    _parse_body_preview,
    _parse_frontmatter,
    _parse_list,
    discover_skills,
)

# ---------------------------------------------------------------------------
# _parse_list
# ---------------------------------------------------------------------------

def test_parse_list_bracket_syntax():
    assert _parse_list("[python, javascript]") == ["python", "javascript"]


def test_parse_list_bare_syntax():
    assert _parse_list("auth, login, session") == ["auth", "login", "session"]


def test_parse_list_empty():
    assert _parse_list("") == []


def test_parse_list_whitespace_only():
    assert _parse_list("  ") == []


# ---------------------------------------------------------------------------
# _parse_frontmatter
# ---------------------------------------------------------------------------

def test_parse_frontmatter_basic():
    text = "---\nname: My Skill\ncategory: security\n---\nBody text ignored."
    fm = _parse_frontmatter(text)
    assert fm["name"] == "My Skill"
    assert fm["category"] == "security"
    assert "Body text ignored." not in fm


def test_parse_frontmatter_no_fence():
    fm = _parse_frontmatter("name: no fence here")
    assert fm == {}


def test_parse_frontmatter_missing_closing_fence():
    text = "---\nname: Partial\ncategory: boilerplate\n"
    fm = _parse_frontmatter(text)
    assert fm["name"] == "Partial"
    assert fm["category"] == "boilerplate"


# ---------------------------------------------------------------------------
# discover_skills
# ---------------------------------------------------------------------------

_FULL_SKILL_MD = """\
---
name: Django Auth Hardening
description: Tighten Django authentication and session security
category: security
keywords: [auth, login, session, django]
languages: [python]
framework: django
---

Long-form body that should be ignored.
"""


def test_discover_skills_happy_path(tmp_path):
    skill_dir = tmp_path / ".openshard" / "skills" / "django-auth"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_FULL_SKILL_MD, encoding="utf-8")

    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s.slug == "django-auth"
    assert s.name == "Django Auth Hardening"
    assert s.category == "security"
    assert s.keywords == ["auth", "login", "session", "django"]
    assert s.languages == ["python"]
    assert s.framework == "django"
    assert s.source == "local"
    assert s.scope == "repository"
    assert s.version == "1.2.0"
    assert s.digest.startswith("sha256:")
    assert len(s.digest) == 71


def test_discover_skills_missing_optional_fields(tmp_path):
    skill_dir = tmp_path / ".openshard" / "skills" / "simple-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Simple\n---\n", encoding="utf-8"
    )

    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s.languages == []
    assert s.framework is None
    assert s.keywords == []
    assert s.category == "standard"
    assert s.version is None
    assert s.digest.startswith("sha256:")


def test_discover_skills_no_skills_dir(tmp_path):
    assert discover_skills(tmp_path) == []


def test_discover_skills_malformed_yaml_skipped(tmp_path):
    skill_dir = tmp_path / ".openshard" / "skills" / "bad-skill"
    skill_dir.mkdir(parents=True)
    # Missing name → skipped
    (skill_dir / "SKILL.md").write_text("---\ncategory: security\n---\n", encoding="utf-8")

    assert discover_skills(tmp_path) == []


def test_discover_skills_multiple_skills(tmp_path):
    for slug, name in [("skill-a", "Skill A"), ("skill-b", "Skill B")]:
        d = tmp_path / ".openshard" / "skills" / slug
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")

    skills = discover_skills(tmp_path)
    assert len(skills) == 2
    slugs = {s.slug for s in skills}
    assert slugs == {"skill-a", "skill-b"}


# ---------------------------------------------------------------------------
# _parse_body_preview
# ---------------------------------------------------------------------------

def test_parse_body_preview_basic():
    text = "---\nname: X\n---\nFirst line.\nSecond line.\nThird line.\nFourth line."
    assert _parse_body_preview(text) == "First line. Second line. Third line."


def test_parse_body_preview_skips_blank_lines():
    text = "---\nname: X\n---\n\nLine one.\n\nLine two."
    assert _parse_body_preview(text) == "Line one. Line two."


def test_parse_body_preview_no_body():
    text = "---\nname: X\n---\n"
    assert _parse_body_preview(text) == ""


def test_parse_body_preview_no_frontmatter():
    assert _parse_body_preview("just plain text") == ""


def test_discover_skills_body_preview_populated(tmp_path):
    skill_dir = tmp_path / ".openshard" / "skills" / "preview-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Preview Skill\n---\nDo this first.\nThen do that.\n",
        encoding="utf-8",
    )
    skills = discover_skills(tmp_path)
    assert skills[0].body_preview == "Do this first. Then do that."


def test_discover_skills_unreadable_file_skipped(tmp_path):
    good_dir = tmp_path / ".openshard" / "skills" / "good-skill"
    good_dir.mkdir(parents=True)
    (good_dir / "SKILL.md").write_text("---\nname: Good\n---\n", encoding="utf-8")

    bad_dir = tmp_path / ".openshard" / "skills" / "bad-skill"
    bad_dir.mkdir(parents=True)
    bad_md = bad_dir / "SKILL.md"
    bad_md.write_bytes(b"\xff\xfe invalid utf-16 garbage")

    # Should silently skip bad file and return the good one
    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].slug == "good-skill"
