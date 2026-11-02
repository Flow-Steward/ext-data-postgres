"""The installed extension contains one database and its supported profiles."""

from pathlib import Path
import json
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_standalone_database_contract():
    manifest = yaml.safe_load((ROOT / "extension.yaml").read_text())
    variant = json.loads((ROOT / "variant.json").read_text())
    profiles = set(variant["prebuilts"])
    assert manifest["version"] == "1.4.0"
    assert manifest["display_name"] == variant["display_name"]
    contract = manifest["runtime"]["extension_contract_v2"]
    assert len(contract["dataset_connectors"]) == 1
    assert len(contract["connection_types"]) == 1
    for schema in [
        contract["connection_types"][0]["config_schema"],
        contract["dataset_connectors"][0]["connection_schema"],
        yaml.safe_load((ROOT / "schemas/config.schema.yaml").read_text()),
    ]:
        assert set(schema["properties"]["prebuilt"]["enum"]) == profiles
        assert not {"account", "warehouse", "role", "dialect", "protocol"} & set(
            schema["properties"]
        )
        for condition in schema["allOf"]:
            assert condition["if"]["properties"]["prebuilt"]["const"] in profiles
    assert not (ROOT / "variants").exists()
    assert not (ROOT / "connector").exists()
    assert not (ROOT / "scripts/render_variant.py").exists()
    provenance = json.loads((ROOT / "toolbox-provenance.json").read_text())
    assert set(provenance["registered_prebuilts"]) == {
        "mysql" if p == "mariadb" else p for p in profiles
    }
