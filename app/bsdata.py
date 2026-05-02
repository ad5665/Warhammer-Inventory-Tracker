from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .db import utc_now_sql

DEFAULT_GAME_SYSTEM = "wh40k_10e"


@dataclass(frozen=True)
class GameSystemConfig:
    id: str
    label: str
    short_label: str
    repo_slug: str
    repo_http_url: str
    repo_git_url: str
    repo_zip_url: str
    branch: str
    catalogue_word: str = "entries"


GAME_SYSTEMS: dict[str, GameSystemConfig] = {
    "wh40k_10e": GameSystemConfig(
        id="wh40k_10e",
        label="Warhammer 40,000 10th Edition",
        short_label="40k",
        repo_slug="wh40k-10e",
        repo_http_url="https://github.com/BSData/wh40k-10e",
        repo_git_url="https://github.com/BSData/wh40k-10e.git",
        repo_zip_url="https://github.com/BSData/wh40k-10e/archive/refs/heads/main.zip",
        branch="main",
        catalogue_word="units/models",
    ),
    "kill_team": GameSystemConfig(
        id="kill_team",
        label="Warhammer 40,000: Kill Team",
        short_label="Kill Team",
        repo_slug="wh40k-killteam",
        repo_http_url="https://github.com/BSData/wh40k-killteam",
        repo_git_url="https://github.com/BSData/wh40k-killteam.git",
        repo_zip_url="https://github.com/BSData/wh40k-killteam/archive/refs/heads/master.zip",
        branch="master",
        catalogue_word="teams/operatives",
    ),
    "age_of_sigmar_4e": GameSystemConfig(
        id="age_of_sigmar_4e",
        label="Warhammer Age of Sigmar 4th Edition",
        short_label="AoS",
        repo_slug="age-of-sigmar-4th",
        repo_http_url="https://github.com/BSData/age-of-sigmar-4th",
        repo_git_url="https://github.com/BSData/age-of-sigmar-4th.git",
        repo_zip_url="https://github.com/BSData/age-of-sigmar-4th/archive/refs/heads/main.zip",
        branch="main",
        catalogue_word="warscrolls/units",
    ),
}


@dataclass
class SyncResult:
    repo_dir: str
    message: str
    used_git: bool


@dataclass
class WargearOption:
    key: str
    name: str
    kind: str | None = None
    stats: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedUnit:
    bs_id: str
    name: str
    faction: str
    catalogue_file: str
    game_system: str = DEFAULT_GAME_SYSTEM
    entry_type: str | None = None
    points: float | None = None
    keywords: list[str] = field(default_factory=list)
    stats: dict[str, str] = field(default_factory=dict)
    wargear_options: list[WargearOption] = field(default_factory=list)


@dataclass
class ImportResult:
    files_scanned: int
    units_imported: int
    errors: list[str]


class UnknownGameSystem(ValueError):
    pass


def get_game_system_config(game_system: str | None) -> GameSystemConfig:
    system_id = (game_system or DEFAULT_GAME_SYSTEM).strip()
    try:
        return GAME_SYSTEMS[system_id]
    except KeyError as exc:
        valid = ", ".join(sorted(GAME_SYSTEMS))
        raise UnknownGameSystem(f"Unknown game system '{system_id}'. Valid values: {valid}") from exc


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
    )
    return completed.stdout.strip()


def _git_available() -> bool:
    return shutil.which("git") is not None


def sync_repository(target_dir: Path, config: GameSystemConfig | None = None) -> SyncResult:
    """Clone or update a BSData repository into target_dir.

    Git is preferred because later syncs are small. If git is not available,
    the configured branch zip is downloaded and unpacked.
    """
    config = config or get_game_system_config(DEFAULT_GAME_SYSTEM)
    target_dir = target_dir.resolve()
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    if _git_available():
        if (target_dir / ".git").exists():
            out = _run(["git", "-C", str(target_dir), "pull", "--ff-only"])
            return SyncResult(str(target_dir), out or "Repository already up to date.", True)

        if target_dir.exists() and any(target_dir.iterdir()):
            # Existing non-git data is usually a previous zip download. Replace
            # it so future syncs can use git.
            shutil.rmtree(target_dir)

        out = _run([
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            config.branch,
            config.repo_git_url,
            str(target_dir),
        ])
        return SyncResult(str(target_dir), out or "Repository cloned.", True)

    # Fallback for systems without git installed.
    with tempfile.TemporaryDirectory(prefix=f"{config.repo_slug}-bsdata-") as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / f"{config.repo_slug}-{config.branch}.zip"
        urllib.request.urlretrieve(config.repo_zip_url, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(tmp_path)
        extracted_roots = [
            p for p in tmp_path.iterdir()
            if p.is_dir() and p.name.startswith(f"{config.repo_slug}-")
        ]
        if not extracted_roots:
            raise RuntimeError("Downloaded BSData zip did not contain the expected root directory.")
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(extracted_roots[0], target_dir)

    return SyncResult(str(target_dir), f"Repository downloaded from the {config.branch} branch zip.", False)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split())


def _element_text(element: ET.Element) -> str:
    return _clean_text("".join(element.itertext()))


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _profile_characteristics(profile: ET.Element) -> dict[str, str]:
    stats: dict[str, str] = {}
    for char in profile.iter():
        if _local_name(char.tag) != "characteristic":
            continue
        key = _clean_text(char.attrib.get("name") or char.attrib.get("typeName") or char.attrib.get("typeId"))
        value = _element_text(char)
        if key and value:
            stats[key] = value
    return stats


def _point_cost(candidates: list[ET.Element]) -> float | None:
    for cost in candidates:
        name = (cost.attrib.get("name") or "").lower()
        type_id = (cost.attrib.get("typeId") or "").lower()
        if "pts" not in name and "point" not in name and "pts" not in type_id and "point" not in type_id:
            continue
        raw = cost.attrib.get("value")
        try:
            return float(raw) if raw is not None else None
        except ValueError:
            continue
    return None


def _direct_cost_candidates(entry: ET.Element) -> list[ET.Element]:
    candidates: list[ET.Element] = []
    for costs_node in _children(entry, "costs"):
        candidates.extend(_children(costs_node, "cost"))
    return candidates


def _direct_cost(entry: ET.Element) -> float | None:
    candidates = _direct_cost_candidates(entry)
    direct_cost = _point_cost(candidates)
    if direct_cost is not None:
        return direct_cost
    # Some BattleScribe files put costs deeper in links. Use a descendant
    # fallback if there was no direct cost.
    if not candidates:
        candidates = [node for node in entry.iter() if _local_name(node.tag) == "cost"]
    return _point_cost(candidates)


def _category_keywords(entry: ET.Element) -> list[str]:
    ignore = {
        "configuration",
        "stratagems",
        "abilities",
        "wargear",
        "selection entries",
        "rules",
        "modifiers",
    }
    seen: set[str] = set()
    keywords: list[str] = []
    for node in entry.iter():
        if _local_name(node.tag) != "categoryLink":
            continue
        name = _clean_text(node.attrib.get("name"))
        if not name:
            continue
        if name.lower() in ignore:
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            keywords.append(name)
    return keywords


def _catalogue_faction_name(root: ET.Element, path: Path) -> str:
    faction = _clean_text(root.attrib.get("name")) or path.stem
    return re.sub(r"\s+-\s+Library$", "", faction).strip() or faction


def _unit_stats(entry: ET.Element) -> dict[str, str]:
    preferred_profile: ET.Element | None = None

    profile_priority = {"unit": 0, "operative": 1, "model": 2, "model statline": 3}
    best_priority = 999
    for node in entry.iter():
        if _local_name(node.tag) != "profile":
            continue
        type_name = (node.attrib.get("typeName") or "").lower()
        priority = profile_priority.get(type_name)
        if priority is not None and priority < best_priority:
            preferred_profile = node
            best_priority = priority

    if preferred_profile is None:
        return {}

    return _profile_characteristics(preferred_profile)


def _profile_id_index(root: ET.Element) -> dict[str, ET.Element]:
    indexed: dict[str, ET.Element] = {}
    for node in root.iter():
        if _local_name(node.tag) != "profile":
            continue
        node_id = node.attrib.get("id")
        if node_id and node_id not in indexed:
            indexed[node_id] = node
    return indexed


def _weapon_kind(profile: ET.Element) -> str:
    type_name = _clean_text(profile.attrib.get("typeName")).lower()
    raw_name = _clean_text(profile.attrib.get("name"))
    if "ranged" in type_name or raw_name.startswith("\u2316"):
        return "Ranged"
    if "melee" in type_name or raw_name.startswith("\u2694"):
        return "Melee"
    return "Weapon"


def _is_weapon_profile(profile: ET.Element) -> bool:
    if _local_name(profile.tag) != "profile":
        return False
    type_name = _clean_text(profile.attrib.get("typeName")).lower()
    return "weapon" in type_name


def _clean_weapon_name(name: str) -> str:
    cleaned = _clean_text(name)
    # BattleScribe files may prefix Kill Team ranged/melee profiles with icons.
    cleaned = cleaned.lstrip(" \t\r\n\u2316\u2694*-:.")
    return _clean_text(cleaned)


def _wargear_key(name: str, kind: str) -> str:
    base = f"{kind}:{name}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    if len(slug) > 52:
        slug = slug[:52].strip("-")
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"{slug or 'wargear'}-{digest}"


def _wargear_option_from_profile(profile: ET.Element, name_override: str | None = None) -> WargearOption | None:
    if not _is_weapon_profile(profile):
        return None
    kind = _weapon_kind(profile)
    raw_name = name_override or profile.attrib.get("name")
    name = _clean_weapon_name(raw_name or "")
    if not name:
        return None
    return WargearOption(key=_wargear_key(name, kind), name=name, kind=kind, stats=_profile_characteristics(profile))


def _merge_wargear_option_lists(*option_lists: list[WargearOption]) -> list[WargearOption]:
    seen: set[str] = set()
    merged: list[WargearOption] = []
    for options in option_lists:
        for option in options:
            if option.key in seen:
                continue
            seen.add(option.key)
            merged.append(option)
    return merged


def _collect_wargear_options(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    profile_index: dict[str, ET.Element],
    visited_entries: set[str] | None = None,
) -> list[WargearOption]:
    visited_entries = visited_entries or set()
    options: list[WargearOption] = []

    entry_id = entry.attrib.get("id") or entry.attrib.get("targetId")
    if entry_id:
        if entry_id in visited_entries:
            return []
        visited_entries.add(entry_id)

    for node in entry.iter():
        local = _local_name(node.tag)
        if local == "profile":
            option = _wargear_option_from_profile(node)
            if option is not None:
                options.append(option)
            continue

        if local == "entryLink":
            target_id = node.attrib.get("targetId")
            target = selection_index.get(target_id or "")
            if target is not None:
                options.extend(_collect_wargear_options(target, selection_index, profile_index, visited_entries))
            continue

        if local in {"infoLink", "profileLink"}:
            target_id = node.attrib.get("targetId")
            profile = profile_index.get(target_id or "")
            if profile is not None:
                option = _wargear_option_from_profile(profile, node.attrib.get("name") or profile.attrib.get("name"))
                if option is not None:
                    options.append(option)

    return _merge_wargear_option_lists(options)


def _wargear_options_json(options: list[WargearOption]) -> str:
    payload = [
        {"key": option.key, "name": option.name, "kind": option.kind, "stats": option.stats}
        for option in options
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _stable_id(catalogue_file: str, name: str, attrs: dict[str, str], game_system: str) -> str:
    raw = attrs.get("id") or attrs.get("targetId")
    if raw:
        # Older app versions had a UNIQUE(bs_id, catalogue_file) constraint.
        # Prefix non-40k imports to avoid collisions when upgrading an existing DB.
        return raw if game_system == DEFAULT_GAME_SYSTEM else f"{game_system}:{raw}"
    digest = hashlib.sha1(f"{game_system}:{catalogue_file}:{name}".encode("utf-8")).hexdigest()[:16]
    return f"generated-{digest}"


def _is_trackable_entry(entry: ET.Element, game_system: str, *, allow_hidden: bool = False) -> bool:
    tag = _local_name(entry.tag)
    if tag not in {"selectionEntry", "entryLink"}:
        return False

    if not allow_hidden and (entry.attrib.get("hidden") or "").lower() == "true":
        return False

    entry_type = (entry.attrib.get("type") or "").lower().strip()
    if entry_type in {"unit", "model"}:
        return True

    # Some Kill Team catalogues use imported entry links or different type
    # labels around operatives. Include obvious operative/team entries, but
    # keep upgrades, rules and wargear out of the searchable catalogue.
    if game_system == "kill_team":
        name = _clean_text(entry.attrib.get("name")).lower()
        categories = {kw.lower() for kw in _category_keywords(entry)}
        if "operative" in categories or "kill team" in categories:
            return True
        if "operative" in name and entry_type not in {"upgrade", "wargear"}:
            return True

    return False


def _entry_id_index(root: ET.Element) -> dict[str, ET.Element]:
    indexed: dict[str, ET.Element] = {}
    for node in root.iter():
        if _local_name(node.tag) != "selectionEntry":
            continue
        node_id = node.attrib.get("id")
        if node_id and node_id not in indexed:
            indexed[node_id] = node
    return indexed


def _merge_entry_link(entry: ET.Element, id_index: dict[str, ET.Element]) -> tuple[ET.Element, bool]:
    if _local_name(entry.tag) != "entryLink":
        return entry, False
    target_id = entry.attrib.get("targetId")
    target = id_index.get(target_id or "")
    if target is None:
        return entry, False
    return target, True


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in list(parent)}


def _has_selection_entry_ancestor(entry: ET.Element, parents: dict[ET.Element, ET.Element]) -> bool:
    parent = parents.get(entry)
    while parent is not None:
        if _local_name(parent.tag) in {"selectionEntry", "entryLink"}:
            return True
        parent = parents.get(parent)
    return False


def parse_catalogue_file(path: Path, game_system: str = DEFAULT_GAME_SYSTEM) -> list[ParsedUnit]:
    config = get_game_system_config(game_system)
    tree = ET.parse(path)
    root = tree.getroot()
    faction = _catalogue_faction_name(root, path)
    catalogue_file = path.name
    units: list[ParsedUnit] = []
    seen_keys: set[tuple[str, str, str]] = set()
    seen_names: set[tuple[str, str, str]] = set()
    id_index = _entry_id_index(root)
    profile_index = _profile_id_index(root)
    parents = _parent_map(root)

    for entry in root.iter():
        source_entry = entry
        data_entry, resolved_link = _merge_entry_link(entry, id_index)

        source_type = (source_entry.attrib.get("type") or "").lower().strip()
        data_type = (data_entry.attrib.get("type") or source_type).lower().strip()
        if data_type == "model" and _has_selection_entry_ancestor(source_entry, parents):
            # Nested model entries are usually individual squad members or
            # options inside a unit. Keep top-level/shared model datasheets
            # such as Chaos Lords and Kill Team operatives, but avoid flooding
            # the catalogue with every internal model option.
            continue

        trackable = _is_trackable_entry(source_entry, config.id)
        if not trackable and resolved_link:
            # Visible entryLinks can point at hidden shared library entries. Use
            # the target for classification so linked units such as Chaos Lord
            # variants are not dropped merely because the library definition is
            # hidden from direct selection.
            trackable = _is_trackable_entry(data_entry, config.id, allow_hidden=True)
        if not trackable:
            continue

        entry_type = (data_entry.attrib.get("type") or source_entry.attrib.get("type") or "").lower().strip() or "entry"
        name = _clean_text(source_entry.attrib.get("name") or data_entry.attrib.get("name"))
        if not name:
            continue

        id_attrs = source_entry.attrib if source_entry.attrib.get("id") else data_entry.attrib
        bs_id = _stable_id(catalogue_file, name, id_attrs, config.id)
        key = (config.id, bs_id, catalogue_file)
        name_key = (config.id, catalogue_file, name.lower())
        if key in seen_keys or name_key in seen_names:
            continue
        seen_keys.add(key)
        seen_names.add(name_key)

        keywords = _category_keywords(data_entry) or _category_keywords(source_entry)
        stats = _unit_stats(data_entry) or _unit_stats(source_entry)
        wargear_options = _merge_wargear_option_lists(
            _collect_wargear_options(data_entry, id_index, profile_index),
            _collect_wargear_options(source_entry, id_index, profile_index),
        )
        points = _direct_cost(data_entry)
        if points is None:
            points = _direct_cost(source_entry)

        units.append(
            ParsedUnit(
                bs_id=bs_id,
                name=name,
                faction=faction,
                catalogue_file=catalogue_file,
                game_system=config.id,
                entry_type=entry_type,
                points=points,
                keywords=keywords,
                stats=stats,
                wargear_options=wargear_options,
            )
        )

    return units


def _upsert_unit(conn: Any, unit: ParsedUnit, imported_at: str) -> None:
    existing = conn.execute(
        """
        SELECT id FROM bsd_units
        WHERE game_system = ? AND bs_id = ? AND catalogue_file = ?
        LIMIT 1
        """,
        (unit.game_system, unit.bs_id, unit.catalogue_file),
    ).fetchone()

    values = (
        unit.game_system,
        unit.bs_id,
        unit.name,
        unit.faction,
        unit.catalogue_file,
        unit.entry_type,
        unit.points,
        ", ".join(unit.keywords),
        json.dumps(unit.stats, ensure_ascii=False, sort_keys=True),
        _wargear_options_json(unit.wargear_options),
        imported_at,
    )

    if existing:
        conn.execute(
            """
            UPDATE bsd_units SET
                game_system = ?,
                bs_id = ?,
                name = ?,
                faction = ?,
                catalogue_file = ?,
                entry_type = ?,
                points = ?,
                keywords = ?,
                stats_json = ?,
                wargear_options_json = ?,
                active = 1,
                imported_at = ?
            WHERE id = ?
            """,
            (*values, existing["id"]),
        )
        return

    conn.execute(
        """
        INSERT INTO bsd_units (
            game_system, bs_id, name, faction, catalogue_file, entry_type, points,
            keywords, stats_json, wargear_options_json, active, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        values,
    )


def _linked_entry_points(cat_files: list[Path], game_system: str) -> dict[str, float]:
    points_by_bs_id: dict[str, float] = {}
    for path in cat_files:
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue

        for entry_link in root.iter():
            if _local_name(entry_link.tag) != "entryLink":
                continue
            target_id = entry_link.attrib.get("targetId")
            if not target_id:
                continue
            points = _point_cost(_direct_cost_candidates(entry_link))
            if points is None:
                continue
            bs_id = _stable_id(path.name, _clean_text(entry_link.attrib.get("name")), {"id": target_id}, game_system)
            points_by_bs_id.setdefault(bs_id, points)

    return points_by_bs_id


def import_bsdata(conn: Any, repo_dir: Path, game_system: str = DEFAULT_GAME_SYSTEM) -> ImportResult:
    config = get_game_system_config(game_system)
    repo_dir = repo_dir.resolve()
    if not repo_dir.exists():
        raise FileNotFoundError(f"BSData directory does not exist: {repo_dir}")

    now = utc_now_sql()
    files_scanned = 0
    units_imported = 0
    errors: list[str] = []

    conn.execute("UPDATE bsd_units SET active = 0 WHERE game_system = ?", (config.id,))

    cat_files = sorted(
        p for p in repo_dir.rglob("*.cat")
        if ".git" not in p.parts and p.is_file()
    )
    linked_points = _linked_entry_points(cat_files, config.id)

    for path in cat_files:
        files_scanned += 1
        try:
            parsed_units = parse_catalogue_file(path, config.id)
        except Exception as exc:  # Keep the import going if one catalogue fails.
            errors.append(f"{path.name}: {exc}")
            continue

        for unit in parsed_units:
            if unit.points is None:
                unit.points = linked_points.get(unit.bs_id)
            _upsert_unit(conn, unit, now)
            units_imported += 1

    return ImportResult(files_scanned=files_scanned, units_imported=units_imported, errors=errors)
