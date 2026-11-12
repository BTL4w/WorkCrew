"""Keys must separate tenant, actor, revision and validated request scope."""

from dataclasses import replace
from uuid import uuid4


def test_cache_keys_separate_authorization_and_source_versions() -> None:
    from app.modules.optimization.application.cache_contracts import (
        CacheRevision,
        CacheScope,
        ReadCacheKey,
    )

    key = ReadCacheKey(
        namespace="test:unit",
        family="workload.weekly",
        dto_version="1",
        scope=CacheScope(organization_id=uuid4(), membership_id=uuid4(), permission_hash="f" * 64),
        revision=CacheRevision(epoch=uuid4(), generation=1),
        query_hash="e" * 64,
    )
    variants = [
        key,
        replace(key, scope=replace(key.scope, organization_id=uuid4())),
        replace(key, scope=replace(key.scope, membership_id=uuid4())),
        replace(key, scope=replace(key.scope, permission_hash="a" * 64)),
        replace(key, revision=replace(key.revision, epoch=uuid4())),
        replace(key, revision=replace(key.revision, generation=2)),
        replace(key, query_hash="b" * 64),
        replace(key, namespace="test:other"),
    ]
    assert len({value.encode() for value in variants}) == 8
    assert str(key.scope.organization_id) in key.encode()
    assert str(key.scope.membership_id) in key.encode()
