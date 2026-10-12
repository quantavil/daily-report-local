"""Private, best-effort disk cache for pipeline artifacts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

CACHE_VERSION = 1


def _normalize_key_part(value: object) -> object:
    if isinstance(value, Enum):
        return {
            "type": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _normalize_key_part(value.value),
        }
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            return value
        return {"type": "float", "value": repr(value)}
    if isinstance(value, bytes):
        return {"type": "bytes", "sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, Path):
        return {"type": "path", "value": str(value)}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("cache key dictionaries must use string keys")
        return {
            "type": "dict",
            "value": {key: _normalize_key_part(value[key]) for key in sorted(value)},
        }
    if isinstance(value, list):
        return {"type": "list", "value": [_normalize_key_part(item) for item in value]}
    if isinstance(value, tuple):
        return {"type": "tuple", "value": [_normalize_key_part(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize_key_part(item) for item in value]
        normalized.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
        return {"type": "set", "value": normalized}
    raise TypeError(f"unsupported cache key part: {type(value).__name__}")


def cache_key(*parts: object) -> str:
    """Return a stable SHA-256 key that includes the cache format version."""
    value = {"version": CACHE_VERSION, "parts": [_normalize_key_part(part) for part in parts]}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PipelineCache:
    """Best-effort cache; filesystem errors turn caching off without failing sync."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._disabled = False

    def get_bytes(self, namespace: str, key: str) -> bytes | None:
        value = self._read(namespace, key, "bytes")
        if not isinstance(value, str):
            return None
        try:
            return base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            return None

    def put_bytes(self, namespace: str, key: str, value: bytes) -> None:
        if not isinstance(value, bytes):
            return
        encoded = base64.b64encode(value).decode("ascii")
        self._write(namespace, key, "bytes", encoded, "bytes")

    def get_json(self, namespace: str, key: str) -> dict[str, Any] | None:
        value = self._read(namespace, key, "json")
        return value if isinstance(value, dict) else None

    def put_json(self, namespace: str, key: str, value: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            return
        self._write(namespace, key, "json", value, "json")

    def _entry_path(self, namespace: str, key: str, suffix: str) -> Path | None:
        if not isinstance(namespace, str) or not isinstance(key, str):
            return None
        namespace_hash = hashlib.sha256(namespace.encode("utf-8", errors="surrogatepass")).hexdigest()
        key_hash = hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).hexdigest()
        return self.root / f"{namespace_hash}-{key_hash}.{suffix}"

    def _ensure_root(self) -> bool:
        if self._disabled:
            return False
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise OSError("cache root is not a real directory")
            self.root.chmod(0o700)
        except OSError:
            self._disabled = True
            return False
        return True

    def _read(self, namespace: str, key: str, kind: str) -> object | None:
        path = self._entry_path(namespace, key, kind)
        if path is None or not self._ensure_root():
            return None
        try:
            if path.is_symlink():
                return None
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeError:
            return None
        except OSError:
            self._disabled = True
            return None
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            return None
        if (
            not isinstance(envelope, dict)
            or envelope.get("version") != CACHE_VERSION
            or envelope.get("kind") != kind
        ):
            return None
        return envelope.get("value")

    def _write(self, namespace: str, key: str, kind: str, value: object, suffix: str) -> None:
        path = self._entry_path(namespace, key, suffix)
        if path is None or not self._ensure_root():
            return
        try:
            encoded = json.dumps(
                {"version": CACHE_VERSION, "kind": kind, "value": value},
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".tmp-", suffix=".tmp", dir=self.root, delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                os.chmod(temporary_path, 0o600)
                temporary_file.write(encoded)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError:
            self._disabled = True
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    self._disabled = True
