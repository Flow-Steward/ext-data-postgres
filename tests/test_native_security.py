"""Check security floors against bytes of the real built payloads."""
import importlib.util
import json
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_security", ROOT / "scripts/native_security.py")
native_security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_security)

@pytest.mark.packaged
@pytest.mark.parametrize("target", ["linux-amd64", "linux-arm64"])
def test_native_binary_security_floor(target):
    binary = ROOT / "bin" / target / "toolbox"
    metadata = ROOT / ".fs-package.yaml"
    if metadata.exists() and not binary.exists():
        pytest.skip("other architecture is excluded from a target package")
    record = native_security.inspect_binary(binary)
    baseline = json.loads((ROOT / "scripts/native-security-baseline.json").read_text())
    native_security.check_floors(record, baseline)
    provenance = json.loads((ROOT / "toolbox-provenance.json").read_text())["binaries"][target]
    for key in ("go_version", "modules", "module_gaps", "module_replacements", "sha256", "size"):
        assert record[key] == provenance[key]

@pytest.mark.parametrize("version", ["v0.57.0", "v0.58.1-0.20261008181718-2f1f834330b2"])
def test_dependency_downgrades_are_rejected(version):
    record = {"go_version": "go1.27.3", "modules": {"golang.org/x/crypto": version}, "module_gaps": []}
    baseline = json.loads((ROOT / "scripts/native-security-baseline.json").read_text())
    with pytest.raises(ValueError, match="module below security floor"):
        native_security.check_floors(record, baseline)


def test_local_replacement_cannot_hide_a_dependency():
    record = {"go_version": "go1.27.3", "modules": {}, "module_gaps": ["unversioned replacement"]}
    baseline = json.loads((ROOT / "scripts/native-security-baseline.json").read_text())
    with pytest.raises(ValueError, match="unversioned native dependencies"):
        native_security.check_floors(record, baseline)

@pytest.mark.parametrize("go_version", ["go1.27.0", "go1.27.1"])
def test_unreviewed_compiler_branch_is_rejected(go_version):
    baseline = json.loads((ROOT / "scripts/native-security-baseline.json").read_text())
    record = {"go_version": go_version, "modules": {}, "module_gaps": []}
    with pytest.raises(ValueError, match="reviewed compiler"):
        native_security.check_floors(record, baseline)


@pytest.mark.parametrize("path,version,accepted", [
    ("example.test/fork", "v99.0.0", False),
    ("golang.org/x/net", "v0.61.0", False),
    ("golang.org/x/net", "v0.62.0", True),
])
def test_replacement_cannot_bypass_original_dependency_floor(tmp_path, path, version, accepted):
    baseline = json.loads((ROOT / "scripts/native-security-baseline.json").read_text())
    import struct
    text = "dep\tgolang.org/x/net\tv0.61.0\th1:old\n=>\t" + path + "\t" + version + "\th1:replacement\n"
    framed = native_security.START + text.encode() + native_security.END
    compiler = b"go1.27.3"
    blob = native_security.MAGIC + bytes([8, 2]) + bytes(16)
    blob += bytes([len(compiler)]) + compiler + bytes([len(framed)]) + framed
    payload = bytearray(256)
    payload[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<HHIQQQIHHHHHH", payload, 16, 2, 62, 1, 0, 64, 128, 0, 64, 56, 1, 64, 2, 0)
    struct.pack_into("<IIQQQQQQ", payload, 64, 1, 6, 256, 4096, 4096, len(blob), len(blob), 16)
    struct.pack_into("<IIQQQQIIQQ", payload, 192, 0, 1, 3, 4096, 256, len(blob), 0, 0, 16, 0)
    binary = tmp_path / "toolbox"
    binary.write_bytes(bytes(payload) + blob)
    record = native_security.inspect_binary(binary)
    if accepted:
        native_security.check_floors(record, baseline)
    else:
        with pytest.raises(ValueError):
            native_security.check_floors(record, baseline)


@pytest.mark.parametrize("target", ["linux-amd64", "linux-arm64"])
def test_packager_rejects_hash_consistent_old_payload(tmp_path, target):
    # Use a real archived payload when running the regression against a chosen root.
    import os
    import shutil
    old_root = os.environ.get("FS_OLD_NATIVE_ROOT")
    if not old_root:
        pytest.skip("requires a verified previous release's native payload")
    old_binary = Path(old_root) / "bin" / target / "toolbox"
    fixture = tmp_path / "extension"
    shutil.copytree(ROOT, fixture, ignore=shutil.ignore_patterns(".git", "bin", "dist", "__pycache__"))
    binary = fixture / "bin" / target / "toolbox"
    binary.parent.mkdir(parents=True)
    shutil.copy2(old_binary, binary)
    record = native_security.inspect_binary(binary)
    record["path"] = f"bin/{target}/toolbox"
    provenance = json.loads((fixture / "toolbox-provenance.json").read_text())
    provenance["binaries"][target] = record
    (fixture / "toolbox-provenance.json").write_text(json.dumps(provenance))
    package_spec = importlib.util.spec_from_file_location("package_extension", fixture / "scripts/package_extension.py")
    package = importlib.util.module_from_spec(package_spec)
    package_spec.loader.exec_module(package)
    with pytest.raises(ValueError, match="reviewed compiler"):
        package.package_extension(fixture, target, tmp_path / "out")
