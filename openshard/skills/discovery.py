from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path


@dataclass
class SkillDef:
    slug: str
    name: str
    description: str
    category: str
    keywords: list[str]
    languages: list[str]
    framework: str | None
    body_preview: str = ""
    # Integrity/provenance metadata. Defaults keep older callers and tests
    # compatible while discovered Skills always populate these fields.
    source: str = "local"
    scope: str = "repository"
    version: str | None = None
    digest: str = ""


def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()
    return result


def _parse_body_preview(text: str, max_lines: int = 3) -> str:
    """Return the first *max_lines* non-blank lines after the closing frontmatter ``---``."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    past_open = False
    body_lines: list[str] = []
    for line in lines[1:]:
        if not past_open:
            if line.strip() == "---":
                past_open = True
            continue
        stripped = line.strip()
        if stripped:
            body_lines.append(stripped)
            if len(body_lines) >= max_lines:
                break
    return " ".join(body_lines)


def discover_skills(root: Path) -> list[SkillDef]:
    """Scan <root>/.openshard/skills/*/SKILL.md and return parsed skill defs.

    Each discovered Skill carries a content digest and explicit local,
    repository-scoped provenance. The digest identifies the exact UTF-8
    SKILL.md bytes Openshard parsed; it does not make the Skill trusted.
    """
    skills_dir = root / ".openshard" / "skills"
    if not skills_dir.is_dir():
        return []
    skills: list[SkillDef] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        try:
            raw = skill_md.read_bytes()
            text = raw.decode("utf-8")
            fm = _parse_frontmatter(text)
            if not fm.get("name"):
                continue
            skills.append(SkillDef(
                slug=skill_md.parent.name,
                name=fm["name"],
                description=fm.get("description", ""),
                category=fm.get("category", "standard"),
                keywords=_parse_list(fm.get("keywords", "")),
                languages=_parse_list(fm.get("languages", "")),
                framework=fm.get("framework") or None,
                body_preview=_parse_body_preview(text),
                source="local",
                scope="repository",
                version=fm.get("version") or None,
                digest="sha256:" + sha256(raw).hexdigest(),
            ))
        except Exception:
            continue
    return skills
