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
class ModelCompositionEntry:
    key: str
    name: str
    min_models: int | None = None
    max_models: int | None = None
    wargear_options: list[WargearOption] = field(default_factory=list)
    composition_options: list[str] = field(default_factory=list)
    display_in_composition: bool = True


@dataclass
class ParsedUnit:
    bs_id: str
    name: str
    faction: str
    catalogue_file: str
    game_system: str = DEFAULT_GAME_SYSTEM
    entry_type: str | None = None
    points: float | None = None
    min_models: int | None = None
    max_models: int | None = None
    keywords: list[str] = field(default_factory=list)
    stats: dict[str, str] = field(default_factory=dict)
    wargear_options: list[WargearOption] = field(default_factory=list)
    model_composition: list[ModelCompositionEntry] = field(default_factory=list)


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


@dataclass(frozen=True)
class _UnitSize:
    min_models: int
    max_models: int | None


def _constraint_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if parsed < 0 or not parsed.is_integer():
        return None
    return int(parsed)


def _selection_constraints(entry: ET.Element) -> tuple[int | None, int | None]:
    minimum: int | None = None
    maximum: int | None = None
    for constraints_node in _children(entry, "constraints"):
        for constraint in _children(constraints_node, "constraint"):
            if (constraint.attrib.get("field") or "").lower() != "selections":
                continue
            if (constraint.attrib.get("scope") or "").lower() != "parent":
                continue
            value = _constraint_int(constraint.attrib.get("value"))
            if value is None:
                continue
            constraint_type = (constraint.attrib.get("type") or "").lower()
            if constraint_type == "min":
                minimum = value if minimum is None else max(minimum, value)
            elif constraint_type == "max":
                maximum = value if maximum is None else min(maximum, value)
    return minimum, maximum


_UNIT_SIZE_RANGE_RE = re.compile(r"(?<![\w])(\d+)\s*(?:-|\u2013|\u2014|\bto\b)\s*(\d+)(?![\w])", re.IGNORECASE)
_UNIT_SIZE_SINGLE_RE = re.compile(r"(?<![\w-])(\d+)(?![\w-])")
_UNIT_SIZE_PREFIX_RE = re.compile(
    r"^\s*\d+\s*(?:(?:-|\u2013|\u2014|\bto\b)\s*\d+)?\s+",
    re.IGNORECASE,
)
_COMMAND_MODEL_RE = re.compile(
    r"\b(?:aspiring champion|champion|sergeant|nob|leader|pack leader|superior|prime|alpha|boss)\b",
    re.IGNORECASE,
)


def _size_from_name(name: str) -> _UnitSize | None:
    cleaned = _clean_text(name)
    if not cleaned:
        return None

    ranges: list[tuple[int, int, tuple[int, int]]] = []
    consumed: list[tuple[int, int]] = []
    for match in _UNIT_SIZE_RANGE_RE.finditer(cleaned):
        low = _constraint_int(match.group(1))
        high = _constraint_int(match.group(2))
        if low is None or high is None or low > high:
            continue
        ranges.append((low, high, match.span()))
        consumed.append(match.span())

    singles: list[int] = []
    for match in _UNIT_SIZE_SINGLE_RE.finditer(cleaned):
        if any(start <= match.start() and match.end() <= end for start, end in consumed):
            continue
        value = _constraint_int(match.group(1))
        if value is not None:
            singles.append(value)

    if not ranges and not singles:
        return None

    minimum = sum(low for low, _, _ in ranges) + sum(singles)
    maximum = sum(high for _, high, _ in ranges) + sum(singles)
    if minimum <= 0 or maximum <= 0:
        return None
    return _UnitSize(minimum, maximum)


def _composition_name(name: str) -> str:
    cleaned = _clean_text(name)
    without_count = _clean_text(_UNIT_SIZE_PREFIX_RE.sub("", cleaned, count=1))
    return without_count or cleaned


def _component_key(name: str, attrs: dict[str, str], min_models: int | None, max_models: int | None) -> str:
    raw_id = attrs.get("id") or attrs.get("targetId") or ""
    base = f"{raw_id}:{name}:{min_models}:{max_models}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if len(slug) > 44:
        slug = slug[:44].strip("-")
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
    return f"{slug or 'model'}-{digest}"


def _is_command_model_name(name: str) -> bool:
    return bool(_COMMAND_MODEL_RE.search(name))


def _is_unit_composition_group(entry: ET.Element) -> bool:
    return (
        _local_name(entry.tag) == "selectionEntryGroup"
        and _clean_text(entry.attrib.get("name")).lower() == "unit composition"
    )


def _direct_selection_children(entry: ET.Element) -> list[ET.Element]:
    children: list[ET.Element] = []
    for container_name in ("selectionEntries", "selectionEntryGroups", "entryLinks"):
        for container in _children(entry, container_name):
            for child in list(container):
                if _local_name(child.tag) in {"selectionEntry", "selectionEntryGroup", "entryLink"}:
                    children.append(child)
    return children


def _entry_group_id_index(root: ET.Element) -> dict[str, ET.Element]:
    indexed: dict[str, ET.Element] = {}
    for node in root.iter():
        if _local_name(node.tag) != "selectionEntryGroup":
            continue
        node_id = node.attrib.get("id")
        if node_id and node_id not in indexed:
            indexed[node_id] = node
    return indexed


def _resolve_selection_reference(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
) -> ET.Element:
    if _local_name(entry.tag) != "entryLink":
        return entry
    target_id = entry.attrib.get("targetId") or ""
    target = group_index.get(target_id)
    if target is not None:
        return target
    target = selection_index.get(target_id)
    return target if target is not None else entry


def _contains_model_entry(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    visited: set[str] | None = None,
) -> bool:
    visited = visited or set()
    for node in entry.iter():
        local = _local_name(node.tag)
        if local == "selectionEntry" and (node.attrib.get("type") or "").lower().strip() == "model":
            return True
        if local == "entryLink":
            target_id = node.attrib.get("targetId") or ""
            if not target_id or target_id in visited:
                continue
            target = group_index.get(target_id)
            if target is None:
                target = selection_index.get(target_id)
            if target is None:
                continue
            target_type = (target.attrib.get("type") or "").lower().strip()
            if target_type == "model" or _contains_model_entry(
                target,
                selection_index,
                group_index,
                visited={*visited, target_id},
            ):
                return True
    return False


def _linked_model_size(
    source: ET.Element,
    target: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
) -> _UnitSize | None:
    minimum, maximum = _selection_constraints(source)
    if minimum is not None or maximum is not None:
        return _UnitSize(minimum or 0, maximum)
    return _unit_size_for_entry(target, selection_index, group_index)


def _unit_composition_choice_size(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
) -> _UnitSize | None:
    child_sizes: list[_UnitSize] = []
    for child in _direct_selection_children(entry):
        target = _resolve_selection_reference(child, selection_index, group_index)
        if _local_name(target.tag) != "selectionEntry":
            continue
        if (target.attrib.get("type") or "").lower().strip() != "model":
            continue
        size = _linked_model_size(child, target, selection_index, group_index)
        if size is not None:
            child_sizes.append(size)

    if not child_sizes:
        return None

    minimum = sum(size.min_models for size in child_sizes)
    maximum = None if any(size.max_models is None for size in child_sizes) else sum(
        size.max_models or 0 for size in child_sizes
    )
    return _UnitSize(minimum, maximum)


def _unit_composition_group_size(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
) -> _UnitSize | None:
    choice_sizes = [
        size
        for child in _direct_selection_children(entry)
        if (size := _unit_composition_choice_size(child, selection_index, group_index)) is not None
    ]
    if not choice_sizes:
        return None

    minimum = min(size.min_models for size in choice_sizes)
    maximum = None if any(size.max_models is None for size in choice_sizes) else max(
        size.max_models or 0 for size in choice_sizes
    )
    return _UnitSize(minimum, maximum)


def _unit_size_for_entry(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    *,
    top_level: bool = False,
    visited: set[str] | None = None,
) -> _UnitSize | None:
    visited = visited or set()
    local = _local_name(entry.tag)

    if local == "entryLink":
        target_id = entry.attrib.get("targetId") or ""
        target = group_index.get(target_id)
        if target is None:
            target = selection_index.get(target_id)
        if target is None:
            return None
        if target_id in visited:
            return None
        return _unit_size_for_entry(
            target,
            selection_index,
            group_index,
            top_level=top_level,
            visited={*visited, target_id},
        )

    entry_type = (entry.attrib.get("type") or "").lower().strip()
    if local == "selectionEntry" and entry_type == "model":
        if top_level:
            return _UnitSize(1, 1)
        minimum, maximum = _selection_constraints(entry)
        return _UnitSize(minimum or 0, maximum if maximum is not None else 1)

    if local == "selectionEntryGroup":
        if not _contains_model_entry(entry, selection_index, group_index):
            return None
        if _is_unit_composition_group(entry):
            unit_composition_size = _unit_composition_group_size(entry, selection_index, group_index)
            if unit_composition_size is not None:
                return unit_composition_size
        minimum, maximum = _selection_constraints(entry)
        if minimum is not None or maximum is not None:
            return _UnitSize(minimum or 0, maximum)
        named_size = _size_from_name(entry.attrib.get("name") or "")
        if named_size is not None:
            return named_size

    child_sizes = [
        size
        for child in _direct_selection_children(entry)
        if (size := _unit_size_for_entry(child, selection_index, group_index, visited=visited)) is not None
    ]
    if not child_sizes:
        return None

    minimum = sum(size.min_models for size in child_sizes)
    maximum = None if any(size.max_models is None for size in child_sizes) else sum(
        size.max_models or 0 for size in child_sizes
    )
    if minimum == 0 and (maximum is None or maximum == 0):
        return None
    return _UnitSize(minimum, maximum)


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
    cleaned = cleaned.lstrip(" \t\r\n\u2316\u2694\u27a4*-:.")
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


def _component_size(entry: ET.Element, selection_index: dict[str, ET.Element], group_index: dict[str, ET.Element]) -> _UnitSize | None:
    if _local_name(entry.tag) == "selectionEntry" and (entry.attrib.get("type") or "").lower().strip() == "model":
        minimum, maximum = _selection_constraints(entry)
        if minimum is None and maximum is None:
            return _UnitSize(1, 1)
        return _UnitSize(minimum or 0, maximum)
    return _unit_size_for_entry(entry, selection_index, group_index)


def _component_from_model(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    profile_index: dict[str, ET.Element],
) -> ModelCompositionEntry | None:
    size = _component_size(entry, selection_index, group_index)
    name = _composition_name(entry.attrib.get("name") or "")
    if not name:
        return None
    return ModelCompositionEntry(
        key=_component_key(name, entry.attrib, size.min_models if size else None, size.max_models if size else None),
        name=name,
        min_models=size.min_models if size else None,
        max_models=size.max_models if size else None,
        wargear_options=_collect_wargear_options(entry, selection_index, profile_index),
    )


def _component_from_linked_model(
    source: ET.Element,
    target: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    profile_index: dict[str, ET.Element],
) -> ModelCompositionEntry | None:
    size = _linked_model_size(source, target, selection_index, group_index)
    name = _composition_name(source.attrib.get("name") or target.attrib.get("name") or "")
    if not name:
        return None
    return ModelCompositionEntry(
        key=_component_key(name, target.attrib or source.attrib, size.min_models if size else None, size.max_models if size else None),
        name=name,
        min_models=size.min_models if size else None,
        max_models=size.max_models if size else None,
        wargear_options=_merge_wargear_option_lists(
            _collect_wargear_options(source, selection_index, profile_index),
            _collect_wargear_options(target, selection_index, profile_index),
        ),
        display_in_composition=False,
    )


def _unit_composition_choice_components(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    profile_index: dict[str, ET.Element],
) -> list[ModelCompositionEntry]:
    components: list[ModelCompositionEntry] = []
    for child in _direct_selection_children(entry):
        target = _resolve_selection_reference(child, selection_index, group_index)
        if _local_name(target.tag) != "selectionEntry":
            continue
        if (target.attrib.get("type") or "").lower().strip() != "model":
            continue
        component = _component_from_linked_model(child, target, selection_index, group_index, profile_index)
        if component is not None:
            components.append(component)
    return components


def _unit_composition_group_components(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    profile_index: dict[str, ET.Element],
) -> list[ModelCompositionEntry]:
    options: list[str] = []
    model_components: dict[str, dict[str, Any]] = {}

    for child in _direct_selection_children(entry):
        label = _clean_text(child.attrib.get("name"))
        components = _unit_composition_choice_components(child, selection_index, group_index, profile_index)
        if not label or not components:
            continue
        options.append(label)

        seen_in_choice: set[str] = set()
        for component in components:
            key = component.name.lower()
            seen_in_choice.add(key)
            record = model_components.setdefault(
                key,
                {
                    "name": component.name,
                    "mins": [],
                    "maxes": [],
                    "present": 0,
                    "wargear_options": [],
                },
            )
            record["mins"].append(component.min_models)
            record["maxes"].append(component.max_models)
            record["wargear_options"] = _merge_wargear_option_lists(
                record["wargear_options"],
                component.wargear_options,
            )

        for key in seen_in_choice:
            model_components[key]["present"] += 1

    if not options:
        return []

    components = [
        ModelCompositionEntry(
            key=_component_key("Unit Composition", entry.attrib, None, None),
            name="Unit Composition",
            composition_options=options,
        )
    ]

    choice_count = len(options)
    for record in model_components.values():
        min_values = [value for value in record["mins"] if value is not None]
        max_values = [value for value in record["maxes"] if value is not None]
        min_models = min(min_values) if min_values else None
        if record["present"] < choice_count and min_models is not None:
            min_models = 0
        max_models = None if len(max_values) != len(record["maxes"]) else max(max_values) if max_values else None
        components.append(
            ModelCompositionEntry(
                key=_component_key(record["name"], entry.attrib, min_models, max_models),
                name=record["name"],
                min_models=min_models,
                max_models=max_models,
                wargear_options=record["wargear_options"],
                display_in_composition=False,
            )
        )

    return components


def _collect_component_wargear_from_children(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    profile_index: dict[str, ET.Element],
    excluded: set[ET.Element] | None = None,
) -> list[WargearOption]:
    excluded = excluded or set()
    options: list[WargearOption] = []
    for child in _direct_selection_children(entry):
        target = _resolve_selection_reference(child, selection_index, group_index)
        if child in excluded or target in excluded:
            continue
        options.extend(_collect_wargear_options(child, selection_index, profile_index))
        if target is not child:
            options.extend(_collect_wargear_options(target, selection_index, profile_index))
    return _merge_wargear_option_lists(options)


def _component_from_group(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    profile_index: dict[str, ET.Element],
) -> list[ModelCompositionEntry]:
    if _is_unit_composition_group(entry):
        unit_composition_components = _unit_composition_group_components(entry, selection_index, group_index, profile_index)
        if unit_composition_components:
            return unit_composition_components

    size = _component_size(entry, selection_index, group_index)
    if size is None:
        return []

    direct_model_children: list[ET.Element] = []
    for child in _direct_selection_children(entry):
        target = _resolve_selection_reference(child, selection_index, group_index)
        if _local_name(target.tag) != "selectionEntry":
            continue
        if (target.attrib.get("type") or "").lower().strip() == "model":
            direct_model_children.append(target)
    command_children = [
        child
        for child in direct_model_children
        if _is_command_model_name(child.attrib.get("name") or "")
        and (child_size := _component_size(child, selection_index, group_index)) is not None
        and child_size.min_models == 1
        and child_size.max_models == 1
    ]

    components = [
        component
        for child in command_children
        if (component := _component_from_model(child, selection_index, group_index, profile_index)) is not None
    ]

    command_min = sum(component.min_models or 0 for component in components)
    command_max = sum(component.max_models or 0 for component in components)
    remaining_min = max(size.min_models - command_min, 0)
    remaining_max = None if size.max_models is None else max(size.max_models - command_max, 0)

    if components and remaining_min == 0 and (remaining_max is None or remaining_max == 0):
        return components

    name = _composition_name(entry.attrib.get("name") or "")
    if not name:
        return components
    excluded = set(command_children)
    group_options = (
        _collect_component_wargear_from_children(entry, selection_index, group_index, profile_index, excluded)
        if excluded
        else _collect_wargear_options(entry, selection_index, profile_index)
    )
    components.append(
        ModelCompositionEntry(
            key=_component_key(name, entry.attrib, remaining_min, remaining_max),
            name=name,
            min_models=remaining_min,
            max_models=remaining_max,
            wargear_options=group_options,
        )
    )
    return components


def _model_composition(
    entry: ET.Element,
    selection_index: dict[str, ET.Element],
    group_index: dict[str, ET.Element],
    profile_index: dict[str, ET.Element],
) -> list[ModelCompositionEntry]:
    components: list[ModelCompositionEntry] = []
    for child in _direct_selection_children(entry):
        target = _resolve_selection_reference(child, selection_index, group_index)
        local = _local_name(target.tag)
        entry_type = (target.attrib.get("type") or "").lower().strip()
        if local == "selectionEntry" and entry_type == "model":
            component = _component_from_model(target, selection_index, group_index, profile_index)
            if component is not None:
                components.append(component)
        elif local == "selectionEntryGroup" and _contains_model_entry(target, selection_index, group_index):
            components.extend(_component_from_group(target, selection_index, group_index, profile_index))

    seen: set[str] = set()
    unique_components: list[ModelCompositionEntry] = []
    for component in components:
        if component.key in seen:
            continue
        seen.add(component.key)
        unique_components.append(component)
    return unique_components


def _wargear_options_json(options: list[WargearOption]) -> str:
    payload = [
        {"key": option.key, "name": option.name, "kind": option.kind, "stats": option.stats}
        for option in options
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _model_composition_json(components: list[ModelCompositionEntry]) -> str:
    payload = [
        {
            "key": component.key,
            "name": component.name,
            "min_models": component.min_models,
            "max_models": component.max_models,
            "wargear_options": [
                {"key": option.key, "name": option.name, "kind": option.kind, "stats": option.stats}
                for option in component.wargear_options
            ],
            "composition_options": component.composition_options,
            "display_in_composition": component.display_in_composition,
        }
        for component in components
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
    group_index = _entry_group_id_index(root)
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
        unit_size = _unit_size_for_entry(data_entry, id_index, group_index, top_level=True)
        if unit_size is None:
            unit_size = _unit_size_for_entry(source_entry, id_index, group_index, top_level=True)
        model_composition = _model_composition(data_entry, id_index, group_index, profile_index)
        if not model_composition:
            model_composition = _model_composition(source_entry, id_index, group_index, profile_index)
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
                min_models=unit_size.min_models if unit_size is not None else None,
                max_models=unit_size.max_models if unit_size is not None else None,
                keywords=keywords,
                stats=stats,
                wargear_options=wargear_options,
                model_composition=model_composition,
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
        unit.min_models,
        unit.max_models,
        ", ".join(unit.keywords),
        json.dumps(unit.stats, ensure_ascii=False, sort_keys=True),
        _wargear_options_json(unit.wargear_options),
        _model_composition_json(unit.model_composition),
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
                min_models = ?,
                max_models = ?,
                keywords = ?,
                stats_json = ?,
                wargear_options_json = ?,
                model_composition_json = ?,
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
            min_models, max_models, keywords, stats_json, wargear_options_json,
            model_composition_json, active, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
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
