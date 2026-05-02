from pathlib import Path

from app.bsdata import _linked_entry_points, parse_catalogue_file


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
    assert "Character" in units[0].keywords
    assert units[0].stats["W"] == "5"


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
