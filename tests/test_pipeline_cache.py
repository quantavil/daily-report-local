from __future__ import annotations

import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from enum import IntEnum, StrEnum
from pathlib import Path

import pipeline_cache
from pipeline_cache import CACHE_VERSION, PipelineCache, cache_key


def test_bytes_and_json_round_trip(tmp_path: Path) -> None:
    cache = PipelineCache(tmp_path / "cache")
    payload = {"leo_date": None, "warnings": ["uncertain date", "kept page 2"]}

    cache.put_bytes("pdf", "attachment-key", b"\x00PDF\xff")
    cache.put_json("ai", "result-key", payload)

    assert cache.get_bytes("pdf", "attachment-key") == b"\x00PDF\xff"
    assert cache.get_json("ai", "result-key") == payload


def test_namespaces_and_data_kinds_are_isolated(tmp_path: Path) -> None:
    cache = PipelineCache(tmp_path / "cache")

    cache.put_bytes("shared", "key", b"bytes")
    cache.put_json("shared", "key", {"kind": "json"})
    cache.put_json("other-namespace", "key", {"namespace": "other"})

    assert cache.get_bytes("shared", "key") == b"bytes"
    assert cache.get_json("shared", "key") == {"kind": "json"}
    assert cache.get_json("other-namespace", "key") == {"namespace": "other"}


def test_corrupt_or_old_version_entries_are_cache_misses(tmp_path: Path) -> None:
    cache = PipelineCache(tmp_path / "cache")
    cache.put_bytes("pdf", "broken", b"pdf bytes")
    cache.put_json("ai", "broken", {"shipper_name": "Example Exporter"})

    byte_file = next((tmp_path / "cache").glob("*.bytes"))
    json_file = next((tmp_path / "cache").glob("*.json"))
    byte_file.write_bytes(b"not a cache envelope")
    json_file.write_text(
        json.dumps({"version": CACHE_VERSION - 1, "kind": "json", "value": {}}),
        encoding="utf-8",
    )

    assert cache.get_bytes("pdf", "broken") is None
    assert cache.get_json("ai", "broken") is None

    cache.put_bytes("pdf", "invalid-base64", b"value")
    byte_file = cache._entry_path("pdf", "invalid-base64", "bytes")
    assert byte_file is not None
    byte_file.write_text(
        json.dumps({"version": CACHE_VERSION, "kind": "bytes", "value": "%%%"}), encoding="utf-8"
    )
    assert cache.get_bytes("pdf", "invalid-base64") is None


def test_cache_keys_are_stable_distinct_and_do_not_expose_parts() -> None:
    sensitive_part = "sk-or-secret-looking-value"

    assert cache_key("attachment", "same-content") == cache_key("attachment", "same-content")
    assert cache_key("attachment", "same-content") != cache_key("attachment", "changed-content")
    assert cache_key(1) != cache_key("1")
    assert sensitive_part not in cache_key("prompt", sensitive_part)
    assert len(cache_key("prompt", sensitive_part)) == hashlib.sha256().digest_size * 2


def test_enum_key_parts_keep_their_type_identity() -> None:
    class NumericField(IntEnum):
        VALUE = 1

    class TextField(StrEnum):
        VALUE = "one"

    assert cache_key(NumericField.VALUE) != cache_key(1)
    assert cache_key(TextField.VALUE) != cache_key("one")


def test_cache_version_participates_in_cache_key(monkeypatch) -> None:
    initial_key = cache_key("same-profile")

    monkeypatch.setattr(pipeline_cache, "CACHE_VERSION", CACHE_VERSION + 1)

    assert cache_key("same-profile") != initial_key


def test_cache_files_and_directory_are_private(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    cache = PipelineCache(root)

    cache.put_json("ai", "private-key", {"value": "private"})

    assert root.stat().st_mode & 0o777 == 0o700
    cache_files = list(root.iterdir())
    assert len(cache_files) == 1
    assert cache_files[0].stat().st_mode & 0o777 == 0o600
    assert "private-key" not in cache_files[0].name
    assert "private-key" not in cache_files[0].read_text(encoding="utf-8")


def test_untrusted_namespace_and_key_cannot_escape_cache_root(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    cache = PipelineCache(root)
    outside = tmp_path / "outside.json"

    cache.put_json("../../outside", "../outside.json", {"safe": True})

    assert cache.get_json("../../outside", "../outside.json") == {"safe": True}
    assert not outside.exists()
    assert all(path.parent == root for path in root.iterdir())


def test_concurrent_replacements_never_leave_partial_entries(tmp_path: Path) -> None:
    cache = PipelineCache(tmp_path / "cache")
    candidates = [bytes([index]) * 8192 for index in range(8)]

    def write_repeatedly(value: bytes) -> None:
        for _ in range(20):
            cache.put_bytes("pdf", "shared", value)

    with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
        list(executor.map(write_repeatedly, candidates))

    result = cache.get_bytes("pdf", "shared")

    assert result in candidates
    assert not list((tmp_path / "cache").glob("*.tmp-*"))


def test_unwritable_storage_safely_disables_cache(tmp_path: Path) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("file blocks the cache directory", encoding="utf-8")
    cache = PipelineCache(root)

    cache.put_bytes("pdf", "key", b"data")
    cache.put_json("ai", "key", {"value": True})

    assert cache.get_bytes("pdf", "key") is None
    assert cache.get_json("ai", "key") is None
    assert root.read_text(encoding="utf-8") == "file blocks the cache directory"


def test_cache_dir_uses_default_and_environment_override_without_loading_dotenv(
    tmp_path: Path, monkeypatch
) -> None:
    import dotenv

    config_path = Path(__file__).parents[1] / "src" / "config.py"
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: None)

    def load_config():
        spec = importlib.util.spec_from_file_location("config_cache_dir_test", config_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    monkeypatch.delenv("DAILY_REPORT_CACHE_DIR", raising=False)
    assert load_config().CACHE_DIR == config_path.parents[1] / ".cache" / "daily-report"

    monkeypatch.setenv("DAILY_REPORT_CACHE_DIR", "")
    assert load_config().CACHE_DIR == config_path.parents[1] / ".cache" / "daily-report"

    custom_dir = tmp_path / "custom cache"
    monkeypatch.setenv("DAILY_REPORT_CACHE_DIR", str(custom_dir))
    assert load_config().CACHE_DIR == custom_dir
