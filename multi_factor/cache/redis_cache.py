# -*- coding: utf-8 -*-
"""Redis + 进程内二级缓存。Redis 不可用时自动降级为内存缓存。"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None  # type: ignore


class _MemoryStore:
    def __init__(self):
        self._data: dict[str, tuple[float, bytes]] = {}

    def get(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if not item:
            return None
        expire_at, payload = item
        if expire_at and expire_at < time.time():
            self._data.pop(key, None)
            return None
        return payload

    def set(self, key: str, payload: bytes, ttl: int | None) -> None:
        expire_at = time.time() + ttl if ttl else 0
        self._data[key] = (expire_at, payload)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class CacheClient:
    """统一缓存客户端：优先 Redis，失败回退内存。"""

    def __init__(self, redis_cfg: dict | None = None):
        self._mem = _MemoryStore()
        self._redis = None
        self._prefix = "stock_algo:"
        self._enabled = False
        if redis_cfg and redis_cfg.get("enabled", True):
            self._prefix = str(redis_cfg.get("key_prefix") or self._prefix)
            self._connect(redis_cfg)

    def _connect(self, cfg: dict) -> None:
        if redis is None:
            logger.warning("未安装 redis 包，使用内存缓存")
            return
        host = cfg.get("host")
        if not host:
            return
        try:
            client = redis.Redis(
                host=host,
                port=int(cfg.get("port", 6379)),
                password=cfg.get("password") or None,
                db=int(cfg.get("database", cfg.get("db", 0))),
                socket_timeout=float(cfg.get("socket_timeout", 5)),
                decode_responses=False,
            )
            client.ping()
            self._redis = client
            self._enabled = True
            logger.info("Redis 缓存已连接 %s:%s db=%s", host, cfg.get("port", 6379), cfg.get("database", 0))
        except Exception as ex:
            logger.warning("Redis 连接失败，降级内存缓存: %s", ex)
            self._redis = None
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get_bytes(self, key: str) -> bytes | None:
        full = self._full_key(key)
        if self._redis:
            try:
                val = self._redis.get(full)
                if val:
                    return val
            except Exception as ex:
                logger.debug("Redis get 失败: %s", ex)
        return self._mem.get(full)

    def set_bytes(self, key: str, payload: bytes, ttl: int = 3600) -> None:
        full = self._full_key(key)
        self._mem.set(full, payload, ttl)
        if self._redis:
            try:
                self._redis.setex(full, ttl, payload)
            except Exception as ex:
                logger.debug("Redis set 失败: %s", ex)

    def get_json(self, key: str) -> Any | None:
        raw = self.get_bytes(key)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return None

    def set_json(self, key: str, data: Any, ttl: int = 3600) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.set_bytes(key, payload, ttl)

    def get_pickle(self, key: str) -> Any | None:
        raw = self.get_bytes(key)
        if not raw:
            return None
        try:
            return pickle.loads(raw)
        except Exception:
            return None

    def set_pickle(self, key: str, obj: Any, ttl: int = 3600) -> None:
        self.set_bytes(key, pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL), ttl)

    def invalidate(self, pattern_suffix: str) -> int:
        """按后缀清理（仅内存；Redis 用 keys 模式删除）。"""
        count = 0
        suffix = self._full_key(pattern_suffix)
        for k in list(self._mem._data.keys()):
            if suffix in k or k.endswith(pattern_suffix):
                self._mem.delete(k)
                count += 1
        if self._redis:
            try:
                for k in self._redis.scan_iter(match=f"{self._prefix}{pattern_suffix}*"):
                    self._redis.delete(k)
                    count += 1
            except Exception:
                pass
        return count


_GLOBAL_CACHE: CacheClient | None = None


def build_redis_cfg_from_ifind(raw: dict | None) -> dict | None:
    if not raw:
        return None
    redis_raw = raw.get("redis")
    if not redis_raw:
        return None
    return {
        "enabled": redis_raw.get("enabled", True),
        "host": redis_raw.get("host"),
        "port": redis_raw.get("port", 6379),
        "password": redis_raw.get("password"),
        "database": redis_raw.get("database", redis_raw.get("db", 0)),
        "key_prefix": redis_raw.get("key_prefix", "stock_algo:"),
        "socket_timeout": redis_raw.get("socket_timeout", 5),
    }


def get_cache(redis_cfg: dict | None = None) -> CacheClient:
    global _GLOBAL_CACHE
    if _GLOBAL_CACHE is None:
        _GLOBAL_CACHE = CacheClient(redis_cfg)
    return _GLOBAL_CACHE


def reset_cache() -> None:
    global _GLOBAL_CACHE
    _GLOBAL_CACHE = None


def cache_key(*parts: Any) -> str:
    text = "|".join(str(p) for p in parts)
    if len(text) <= 120:
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
