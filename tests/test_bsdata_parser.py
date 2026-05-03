from pathlib import Path
import sys
import zipfile
from xml.etree import ElementTree as ET

import app.db as db
from app import bsdata
from app.bsdata import _linked_entry_points, import_bsdata, parse_catalogue_file, sync_repository


def test_sync_repository_uses_git_pull_clone_and_replaces_non_git_data(tmp_path: Path, monkeypatch):
    calls = []

    def fake_run(cmd, cwd=None):
        calls.append((cmd, cwd))
        return "pulled" if "pull" in cmd else ""

    monkeypatch.setattr(bsdata, "_git_available", lambda: True)
    monkeypatch.setattr(bsdata, "_run", fake_run)

    git_target = tmp_path / "repo"
    (git_target / ".git").mkdir(parents=True)
    pulled = sync_repository(git_target)

    assert pulled.used_git is True
    assert pulled.message == "pulled"
    assert calls[-1][0] == ["git", "-C", str(git_target.resolve()), "pull", "--ff-only"]

    non_git_target = tmp_path / "non-git"
    non_git_target.mkdir()
    (non_git_target / "old-download.txt").write_text("old", encoding="utf-8")
    cloned = sync_repository(non_git_target)

    assert cloned.used_git is True
    assert cloned.message == "Repository cloned."
    assert calls[-1][0][0:2] == ["git", "clone"]
    assert str(non_git_target.resolve()) == calls[-1][0][-1]
    assert not (non_git_target / "old-download.txt").exists()


def test_sync_repository_downloads_zip_when_git_is_unavailable(tmp_path: Path, monkeypatch):
    config = bsdata.get_game_system_config("kill_team")

    def fake_urlretrieve(url, filename):
        assert url == config.repo_zip_url
        with zipfile.ZipFile(filename, "w") as archive:
            archive.writestr(f"{config.repo_slug}-{config.branch}/Kill Team.cat", "<catalogue />")
        return filename, None

    monkeypatch.setattr(bsdata, "_git_available", lambda: False)
    monkeypatch.setattr(bsdata.urllib.request, "urlretrieve", fake_urlretrieve)

    target = tmp_path / "kill-team"
    target.mkdir()
    (target / "old-download.txt").write_text("old", encoding="utf-8")
    result = sync_repository(target, config)

    assert result.used_git is False
    assert result.message == "Repository downloaded from the master branch zip."
    assert (target / "Kill Team.cat").read_text(encoding="utf-8") == "<catalogue />"
    assert not (target / "old-download.txt").exists()


def test_sync_repository_rejects_zip_without_expected_root(tmp_path: Path, monkeypatch):
    config = bsdata.get_game_system_config("kill_team")

    def fake_urlretrieve(url, filename):
        with zipfile.ZipFile(filename, "w") as archive:
            archive.writestr("unexpected-root/Kill Team.cat", "<catalogue />")
        return filename, None

    monkeypatch.setattr(bsdata, "_git_available", lambda: False)
    monkeypatch.setattr(bsdata.urllib.request, "urlretrieve", fake_urlretrieve)

    try:
        sync_repository(tmp_path / "kill-team", config)
    except RuntimeError as exc:
        assert "expected root directory" in str(exc)
    else:
        raise AssertionError("Expected sync_repository to reject an invalid archive")


def test_bsdata_helper_edge_cases():
    assert bsdata._run([sys.executable, "-c", "print('ok')"]) == "ok"
    assert isinstance(bsdata._git_available(), bool)
    assert bsdata._local_name("plain") == "plain"
    assert bsdata._clean_text(None) == ""

    costs = ET.fromstring(
        """
        <root>
          <cost name="power" typeId="power" value="1" />
          <cost name="pts" typeId="points" value="bad" />
        </root>
        """
    )
    assert bsdata._point_cost(list(costs)) is None

    nested_cost = ET.fromstring(
        """
        <selectionEntry>
          <entryLinks>
            <entryLink>
              <costs><cost name="pts" typeId="points" value="42" /></costs>
            </entryLink>
          </entryLinks>
        </selectionEntry>
        """
    )
    assert bsdata._direct_cost(nested_cost) == 42

    assert bsdata._constraint_int(None) is None
    assert bsdata._constraint_int("bad") is None
    assert bsdata._constraint_int("-1") is None
    assert bsdata._constraint_int("1.5") is None

    constraints = ET.fromstring(
        """
        <selectionEntry>
          <constraints>
            <constraint type="min" value="1" field="models" scope="parent" />
            <constraint type="max" value="bad" field="selections" scope="parent" />
            <constraint type="min" value="2" field="selections" scope="roster" />
          </constraints>
        </selectionEntry>
        """
    )
    assert bsdata._selection_constraints(constraints) == (None, None)

    assert bsdata._size_from_name("") is None
    size = bsdata._size_from_name("2 to 4 Bodyguards and 1 Leader")
    assert (size.min_models, size.max_models) == (3, 5)
    assert bsdata._size_from_name("0 Servitors") is None
    assert bsdata._composition_name("10-20 Termagants") == "Termagants"

    key = bsdata._component_key("A" * 80, {"id": "model-id"}, 1, 1)
    assert key.startswith("a" * 44)


def test_import_bsdata_records_errors_assigns_linked_points_and_upserts(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "stock_tracker.db")
    db.init_db()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    unit_path = repo_dir / "Units.cat"
    unit_path.write_text(
        '''<?xml version="1.0" encoding="UTF-8"?>
        <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
          <selectionEntries>
            <selectionEntry id="unit-1" name="Original Squad" type="unit" />
          </selectionEntries>
        </catalogue>
        ''',
        encoding="utf-8",
    )
    (repo_dir / "Links.cat").write_text(
        '''<?xml version="1.0" encoding="UTF-8"?>
        <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
          <entryLinks>
            <entryLink id="link-unit" name="Original Squad" type="selectionEntry" targetId="unit-1">
              <costs><cost name="pts" typeId="points" value="125" /></costs>
            </entryLink>
          </entryLinks>
        </catalogue>
        ''',
        encoding="utf-8",
    )
    (repo_dir / "Broken.cat").write_text("<catalogue>", encoding="utf-8")

    with db.connect() as conn:
        first = import_bsdata(conn, repo_dir)

    assert first.files_scanned == 3
    assert first.units_imported == 1
    assert first.errors and first.errors[0].startswith("Broken.cat:")

    unit_path.write_text(
        '''<?xml version="1.0" encoding="UTF-8"?>
        <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
          <selectionEntries>
            <selectionEntry id="unit-1" name="Updated Squad" type="unit" />
          </selectionEntries>
        </catalogue>
        ''',
        encoding="utf-8",
    )
    with db.connect() as conn:
        second = import_bsdata(conn, repo_dir)
        rows = conn.execute(
            "SELECT name, points, active FROM bsd_units WHERE bs_id = 'unit-1'"
        ).fetchall()

    assert second.units_imported == 1
    assert len(rows) == 1
    assert rows[0]["name"] == "Updated Squad"
    assert rows[0]["points"] == 125
    assert rows[0]["active"] == 1


def test_parse_catalogue_file_reads_unit_entries(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
      <selectionEntries>
        <selectionEntry id="unit-1" name="Test Marines" type="unit">
          <costs><cost name="pts" typeId="points" value="90" /></costs>
          <categoryLinks>
            <categoryLink name="Infantry" />
            <categoryLink name="Battleline" />
          </categoryLinks>
          <profiles>
            <profile name="Test Marine" typeName="Unit">
              <characteristics>
                <characteristic name="M">6&quot;</characteristic>
                <characteristic name="T">4</characteristic>
                <characteristic name="SV">3+</characteristic>
              </characteristics>
            </profile>
          </profiles>
        </selectionEntry>
        <selectionEntry id="upgrade-1" name="Plasma gun" type="upgrade" />
      </selectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Test.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    assert units[0].game_system == "wh40k_10e"
    assert units[0].bs_id == "unit-1"
    assert units[0].name == "Test Marines"
    assert units[0].faction == "Test Faction"
    assert units[0].points == 90
    assert "Infantry" in units[0].keywords
    assert units[0].stats["T"] == "4"


def test_parse_catalogue_file_reads_model_entries_for_characters(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Chaos - Chaos Space Marines">
      <sharedSelectionEntries>
        <selectionEntry id="chaos-lord-1" name="Chaos Lord" type="model">
          <categoryLinks>
            <categoryLink name="Character" />
            <categoryLink name="Infantry" />
          </categoryLinks>
          <profiles>
            <profile name="Chaos Lord" typeName="Model">
              <characteristics>
                <characteristic name="M">6&quot;</characteristic>
                <characteristic name="T">4</characteristic>
                <characteristic name="SV">3+</characteristic>
                <characteristic name="W">5</characteristic>
              </characteristics>
            </profile>
          </profiles>
        </selectionEntry>
      </sharedSelectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Chaos - Chaos Space Marines.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    assert units[0].name == "Chaos Lord"
    assert units[0].entry_type == "model"
    assert units[0].min_models == 1
    assert units[0].max_models == 1
    assert "Character" in units[0].keywords
    assert units[0].stats["W"] == "5"


def test_parse_catalogue_file_reads_unit_size_from_model_group_constraints(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
      <selectionEntries>
        <selectionEntry id="unit-1" name="Intercessor Squad" type="unit">
          <selectionEntryGroups>
            <selectionEntryGroup id="models-1" name="Intercessors">
              <selectionEntries>
                <selectionEntry id="sergeant-1" name="Intercessor Sergeant" type="model">
                  <constraints>
                    <constraint type="min" value="1" field="selections" scope="parent" />
                    <constraint type="max" value="1" field="selections" scope="parent" />
                  </constraints>
                </selectionEntry>
                <selectionEntry id="model-1" name="Intercessor" type="model">
                  <constraints>
                    <constraint type="min" value="4" field="selections" scope="parent" />
                    <constraint type="max" value="9" field="selections" scope="parent" />
                  </constraints>
                </selectionEntry>
              </selectionEntries>
              <constraints>
                <constraint type="min" value="5" field="selections" scope="parent" />
                <constraint type="max" value="10" field="selections" scope="parent" />
              </constraints>
            </selectionEntryGroup>
          </selectionEntryGroups>
        </selectionEntry>
      </selectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Test.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    assert units[0].min_models == 5
    assert units[0].max_models == 10


def test_parse_catalogue_file_sums_separate_model_groups_for_unit_size(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
      <selectionEntries>
        <selectionEntry id="unit-1" name="Boyz" type="unit">
          <selectionEntryGroups>
            <selectionEntryGroup id="boyz-1" name="9-19 Boyz">
              <selectionEntries>
                <selectionEntry id="boy-1" name="Boy" type="model" />
              </selectionEntries>
              <constraints>
                <constraint type="min" value="9" field="selections" scope="parent" />
                <constraint type="max" value="19" field="selections" scope="parent" />
              </constraints>
            </selectionEntryGroup>
            <selectionEntryGroup id="nob-1" name="Boss Nob">
              <selectionEntries>
                <selectionEntry id="boss-nob-1" name="Boss Nob" type="model">
                  <constraints>
                    <constraint type="min" value="1" field="selections" scope="parent" />
                    <constraint type="max" value="1" field="selections" scope="parent" />
                  </constraints>
                </selectionEntry>
              </selectionEntries>
            </selectionEntryGroup>
          </selectionEntryGroups>
        </selectionEntry>
      </selectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Orks.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    assert units[0].min_models == 10
    assert units[0].max_models == 20


def test_parse_catalogue_file_uses_numeric_model_group_name_for_unit_size(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
      <selectionEntries>
        <selectionEntry id="unit-1" name="Termagants" type="unit">
          <selectionEntryGroups>
            <selectionEntryGroup id="termagants-1" name="10-20 Termagants">
              <selectionEntries>
                <selectionEntry id="termagant-1" name="Termagant" type="model">
                  <constraints>
                    <constraint type="min" value="7" field="selections" scope="parent" />
                    <constraint type="max" value="20" field="selections" scope="parent" />
                  </constraints>
                </selectionEntry>
              </selectionEntries>
            </selectionEntryGroup>
          </selectionEntryGroups>
        </selectionEntry>
      </selectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Tyranids.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    assert units[0].min_models == 10
    assert units[0].max_models == 20


def test_parse_catalogue_file_reads_model_composition_and_model_wargear(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Chaos - Chaos Space Marines">
      <selectionEntries>
        <selectionEntry id="legionaries-1" name="Legionaries" type="unit">
          <selectionEntries>
            <selectionEntry id="champion-1" name="Aspiring Champion" type="model">
              <constraints>
                <constraint type="min" value="1" field="selections" scope="parent" />
                <constraint type="max" value="1" field="selections" scope="parent" />
              </constraints>
              <selectionEntryGroups>
                <selectionEntryGroup id="champion-wargear" name="Wargear">
                  <selectionEntries>
                    <selectionEntry id="plasma-pistol" name="Plasma pistol" type="upgrade">
                      <profiles>
                        <profile name="Plasma pistol" typeName="Ranged Weapons">
                          <characteristics><characteristic name="Range">12&quot;</characteristic></characteristics>
                        </profile>
                      </profiles>
                    </selectionEntry>
                    <selectionEntry id="heavy-melee" name="Heavy melee weapon" type="upgrade">
                      <profiles>
                        <profile name="Heavy melee weapon" typeName="Melee Weapons">
                          <characteristics><characteristic name="A">3</characteristic></characteristics>
                        </profile>
                      </profiles>
                    </selectionEntry>
                  </selectionEntries>
                </selectionEntryGroup>
              </selectionEntryGroups>
            </selectionEntry>
          </selectionEntries>
          <selectionEntryGroups>
            <selectionEntryGroup id="legionary-group" name="4 - 9 Legionaries">
              <constraints>
                <constraint type="min" value="4" field="selections" scope="parent" />
                <constraint type="max" value="9" field="selections" scope="parent" />
              </constraints>
              <selectionEntries>
                <selectionEntry id="legionary-1" name="Legionary w/ boltgun" type="model">
                  <profiles>
                    <profile name="Boltgun" typeName="Ranged Weapons">
                      <characteristics><characteristic name="Range">24&quot;</characteristic></characteristics>
                    </profile>
                    <profile name="Close combat weapon" typeName="Melee Weapons">
                      <characteristics><characteristic name="A">3</characteristic></characteristics>
                    </profile>
                  </profiles>
                </selectionEntry>
              </selectionEntries>
            </selectionEntryGroup>
          </selectionEntryGroups>
        </selectionEntry>
      </selectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Chaos - Chaos Space Marines.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    assert units[0].min_models == 5
    assert units[0].max_models == 10
    assert [(component.name, component.min_models, component.max_models) for component in units[0].model_composition] == [
        ("Aspiring Champion", 1, 1),
        ("Legionaries", 4, 9),
    ]
    by_name = {component.name: component for component in units[0].model_composition}
    assert {option.name for option in by_name["Aspiring Champion"].wargear_options} == {
        "Plasma pistol",
        "Heavy melee weapon",
    }
    assert {option.name for option in by_name["Legionaries"].wargear_options} == {
        "Boltgun",
        "Close combat weapon",
    }


def test_parse_catalogue_file_reads_unit_composition_choices(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Xenos - Orks">
      <selectionEntries>
        <selectionEntry id="gretchin-unit" name="Gretchin" type="unit">
          <selectionEntryGroups>
            <selectionEntryGroup id="unit-composition" name="Unit Composition">
              <selectionEntries>
                <selectionEntry id="small-mob" name="1 Runtherd and 10 Gretchin" type="upgrade">
                  <entryLinks>
                    <entryLink id="small-gretchin" name="Gretchin" type="selectionEntry" targetId="gretchin-model">
                      <constraints>
                        <constraint type="min" value="10" field="selections" scope="parent" />
                        <constraint type="max" value="10" field="selections" scope="parent" />
                      </constraints>
                    </entryLink>
                    <entryLink id="small-runtherd" name="Runtherd" type="selectionEntry" targetId="runtherd-model">
                      <constraints>
                        <constraint type="min" value="1" field="selections" scope="parent" />
                        <constraint type="max" value="1" field="selections" scope="parent" />
                      </constraints>
                    </entryLink>
                  </entryLinks>
                </selectionEntry>
                <selectionEntry id="large-mob" name="2 Runtherds and 20 Gretchin" type="upgrade">
                  <entryLinks>
                    <entryLink id="large-gretchin" name="Gretchin" type="selectionEntry" targetId="gretchin-model">
                      <constraints>
                        <constraint type="min" value="20" field="selections" scope="parent" />
                        <constraint type="max" value="20" field="selections" scope="parent" />
                      </constraints>
                    </entryLink>
                    <entryLink id="large-runtherd" name="Runtherd" type="selectionEntry" targetId="runtherd-model">
                      <constraints>
                        <constraint type="min" value="2" field="selections" scope="parent" />
                        <constraint type="max" value="2" field="selections" scope="parent" />
                      </constraints>
                    </entryLink>
                  </entryLinks>
                </selectionEntry>
              </selectionEntries>
              <constraints>
                <constraint type="min" value="1" field="selections" scope="parent" />
                <constraint type="max" value="1" field="selections" scope="parent" />
              </constraints>
            </selectionEntryGroup>
          </selectionEntryGroups>
        </selectionEntry>
      </selectionEntries>
      <sharedSelectionEntries>
        <selectionEntry id="gretchin-model" name="Gretchin" type="model">
          <profiles>
            <profile name="Grot blasta" typeName="Ranged Weapons">
              <characteristics><characteristic name="Range">12&quot;</characteristic></characteristics>
            </profile>
          </profiles>
        </selectionEntry>
        <selectionEntry id="runtherd-model" name="Runtherd" type="model">
          <profiles>
            <profile name="Grot-smacka" typeName="Melee Weapons">
              <characteristics><characteristic name="A">3</characteristic></characteristics>
            </profile>
          </profiles>
        </selectionEntry>
      </sharedSelectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Orks.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    unit = next(unit for unit in units if unit.name == "Gretchin")
    assert unit.min_models == 11
    assert unit.max_models == 22
    visible = [component for component in unit.model_composition if component.display_in_composition]
    assert len(visible) == 1
    assert visible[0].name == "Unit Composition"
    assert visible[0].composition_options == [
        "1 Runtherd and 10 Gretchin",
        "2 Runtherds and 20 Gretchin",
    ]
    by_name = {component.name: component for component in unit.model_composition}
    assert by_name["Gretchin"].min_models == 10
    assert by_name["Gretchin"].max_models == 20
    assert not by_name["Gretchin"].display_in_composition
    assert {option.name for option in by_name["Gretchin"].wargear_options} == {"Grot blasta"}
    assert by_name["Runtherd"].min_models == 1
    assert by_name["Runtherd"].max_models == 2
    assert not by_name["Runtherd"].display_in_composition
    assert {option.name for option in by_name["Runtherd"].wargear_options} == {"Grot-smacka"}


def test_parse_catalogue_file_prefixes_kill_team_ids(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="2024 - Legionaries">
      <selectionEntries>
        <selectionEntry id="operative-1" name="Aspiring Champion" type="model">
          <categoryLinks>
            <categoryLink name="Operative" />
          </categoryLinks>
          <profiles>
            <profile name="Aspiring Champion" typeName="Operative">
              <characteristics>
                <characteristic name="M">6&quot;</characteristic>
                <characteristic name="APL">3</characteristic>
                <characteristic name="W">14</characteristic>
              </characteristics>
            </profile>
          </profiles>
        </selectionEntry>
      </selectionEntries>
    </catalogue>
    '''
    path = tmp_path / "2024 - Legionaries.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path, "kill_team")

    assert len(units) == 1
    assert units[0].game_system == "kill_team"
    assert units[0].bs_id == "kill_team:operative-1"
    assert units[0].name == "Aspiring Champion"
    assert units[0].stats["APL"] == "3"


def test_parse_catalogue_file_reads_age_of_sigmar_units(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Stormcast Eternals - Library">
      <sharedSelectionEntries>
        <selectionEntry id="liberators-1" name="Liberators" type="unit">
          <costs><cost name="pts" typeId="points" value="110" /></costs>
          <categoryLinks>
            <categoryLink name="Infantry" />
            <categoryLink name="Warrior Chamber" />
          </categoryLinks>
          <profiles>
            <profile id="unit-profile" name="Liberators" typeName="Unit">
              <characteristics>
                <characteristic name="Move">5&quot;</characteristic>
                <characteristic name="Health">2</characteristic>
                <characteristic name="Save">3+</characteristic>
                <characteristic name="Control">1</characteristic>
              </characteristics>
            </profile>
            <profile id="weapon-profile" name="Warhammer" typeName="Melee Weapon">
              <characteristics>
                <characteristic name="Atk">2</characteristic>
                <characteristic name="Hit">3+</characteristic>
                <characteristic name="Wnd">3+</characteristic>
                <characteristic name="Rnd">1</characteristic>
                <characteristic name="Dmg">1</characteristic>
              </characteristics>
            </profile>
          </profiles>
        </selectionEntry>
      </sharedSelectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Stormcast Eternals - Library.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path, "age_of_sigmar_4e")

    assert len(units) == 1
    assert units[0].game_system == "age_of_sigmar_4e"
    assert units[0].bs_id == "age_of_sigmar_4e:liberators-1"
    assert units[0].name == "Liberators"
    assert units[0].faction == "Stormcast Eternals"
    assert units[0].points == 110
    assert units[0].stats["Health"] == "2"
    assert units[0].stats["Control"] == "1"
    assert units[0].wargear_options[0].name == "Warhammer"
    assert units[0].wargear_options[0].kind == "Melee"
    assert units[0].wargear_options[0].stats["Hit"] == "3+"


def test_linked_entry_points_reads_age_of_sigmar_costs(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Stormcast Eternals">
      <entryLinks>
        <entryLink id="link-liberators" name="Liberators" type="selectionEntry" targetId="liberators-1">
          <costs><cost name="pts" typeId="points" value="90" /></costs>
        </entryLink>
      </entryLinks>
    </catalogue>
    '''
    path = tmp_path / "Stormcast Eternals.cat"
    path.write_text(sample, encoding="utf-8")

    points = _linked_entry_points([path], "age_of_sigmar_4e")

    assert points["age_of_sigmar_4e:liberators-1"] == 90


def test_parse_catalogue_file_resolves_visible_entry_link_to_hidden_character(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Chaos - Chaos Space Marines">
      <entryLinks>
        <entryLink id="link-chaos-lord" name="Chaos Lord" targetId="shared-chaos-lord" type="selectionEntry" />
      </entryLinks>
      <sharedSelectionEntries>
        <selectionEntry id="shared-chaos-lord" name="Chaos Lord" type="model" hidden="true">
          <categoryLinks>
            <categoryLink name="Character" />
            <categoryLink name="Infantry" />
          </categoryLinks>
          <profiles>
            <profile name="Chaos Lord" typeName="Model">
              <characteristics>
                <characteristic name="W">5</characteristic>
              </characteristics>
            </profile>
          </profiles>
        </selectionEntry>
      </sharedSelectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Chaos - Chaos Space Marines.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert [unit.name for unit in units] == ["Chaos Lord"]
    assert units[0].bs_id == "link-chaos-lord"
    assert units[0].stats["W"] == "5"


def test_parse_catalogue_file_reads_weapon_profiles_as_wargear_options(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
      <selectionEntries>
        <selectionEntry id="unit-weapon-1" name="Weapon Test Squad" type="unit">
          <profiles>
            <profile id="boltgun-profile" name="Boltgun" typeName="Ranged Weapons">
              <characteristics>
                <characteristic name="Range">24&quot;</characteristic>
                <characteristic name="A">2</characteristic>
                <characteristic name="BS">3+</characteristic>
                <characteristic name="S">4</characteristic>
                <characteristic name="AP">0</characteristic>
                <characteristic name="D">1</characteristic>
              </characteristics>
            </profile>
            <profile id="combat-knife-profile" name="Combat knife" typeName="Melee Weapons">
              <characteristics>
                <characteristic name="Range">Melee</characteristic>
                <characteristic name="A">3</characteristic>
              </characteristics>
            </profile>
            <profile id="unit-profile" name="Weapon Test Squad" typeName="Unit">
              <characteristics><characteristic name="T">4</characteristic></characteristics>
            </profile>
          </profiles>
        </selectionEntry>
      </selectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Weapons.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    assert [option.name for option in units[0].wargear_options] == ["Boltgun", "Combat knife"]
    assert units[0].wargear_options[0].stats["Range"] == '24"'


def test_parse_catalogue_file_resolves_linked_weapon_profiles(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
      <selectionEntries>
        <selectionEntry id="linked-unit" name="Linked Weapon Squad" type="unit">
          <selectionEntries>
            <selectionEntry id="model-1" name="Gunner" type="model">
              <profileLinks>
                <profileLink id="link-profile" name="Plasma gun" targetId="plasma-profile" type="profile" />
              </profileLinks>
            </selectionEntry>
          </selectionEntries>
        </selectionEntry>
      </selectionEntries>
      <sharedProfiles>
        <profile id="plasma-profile" name="Plasma gun" typeName="Ranged Weapons">
          <characteristics>
            <characteristic name="Range">24&quot;</characteristic>
            <characteristic name="A">1</characteristic>
            <characteristic name="S">8</characteristic>
          </characteristics>
        </profile>
      </sharedProfiles>
    </catalogue>
    '''
    path = tmp_path / "Linked Weapons.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    assert units[0].wargear_options[0].name == "Plasma gun"
    assert units[0].wargear_options[0].stats["S"] == "8"


def test_parse_catalogue_file_collects_weapon_profiles_and_entry_links(tmp_path: Path):
    sample = '''<?xml version="1.0" encoding="UTF-8"?>
    <catalogue xmlns="http://www.battlescribe.net/schema/catalogueSchema" name="Test Faction">
      <selectionEntries>
        <selectionEntry id="unit-1" name="Test Marines" type="unit">
          <profiles>
            <profile name="Test Marines" typeName="Unit">
              <characteristics><characteristic name="T">4</characteristic></characteristics>
            </profile>
          </profiles>
          <selectionEntries>
            <selectionEntry id="boltgun-entry" name="Boltgun" type="upgrade">
              <profiles>
                <profile id="boltgun-profile" name="Boltgun" typeName="Ranged Weapons">
                  <characteristics>
                    <characteristic name="Range">24&quot;</characteristic>
                    <characteristic name="A">2</characteristic>
                  </characteristics>
                </profile>
              </profiles>
            </selectionEntry>
          </selectionEntries>
          <entryLinks>
            <entryLink id="chainsword-link" name="Chainsword" targetId="chainsword-entry" type="selectionEntry" />
          </entryLinks>
        </selectionEntry>
      </selectionEntries>
      <sharedSelectionEntries>
        <selectionEntry id="chainsword-entry" name="Chainsword" type="upgrade">
          <profiles>
            <profile id="chainsword-profile" name="Chainsword" typeName="Melee Weapons">
              <characteristics><characteristic name="A">4</characteristic></characteristics>
            </profile>
          </profiles>
        </selectionEntry>
      </sharedSelectionEntries>
    </catalogue>
    '''
    path = tmp_path / "Test.cat"
    path.write_text(sample, encoding="utf-8")

    units = parse_catalogue_file(path)

    assert len(units) == 1
    names = {option.name for option in units[0].wargear_options}
    assert {"Boltgun", "Chainsword"}.issubset(names)
    by_name = {option.name: option for option in units[0].wargear_options}
    assert by_name["Boltgun"].kind == "Ranged"
    assert by_name["Chainsword"].kind == "Melee"
    assert by_name["Boltgun"].stats["Range"] == "24\""
