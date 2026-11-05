# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Integration tests for the PostgreSQL ledger backend."""

from __future__ import annotations

import pytest

from nomos.crypto.signer_provider import InMemorySigner
from nomos.level_0.crypto.signing import KeyPair
from nomos.level_1.ledger import (
    LedgerAppendError,
    LedgerChain,
    LedgerEntryType,
)
from tests.postgres_test_support import (
    ensure_migrated_and_clean,
    postgres_dsn_for_tests,
    postgres_unavailable_reason,
)

_PG_SKIP = postgres_unavailable_reason()
pytestmark = pytest.mark.skipif(_PG_SKIP is not None, reason=_PG_SKIP or "")

CHAIN_ID = "ledger-council-pg-test"


@pytest.fixture
def pool():
    from psycopg_pool import ConnectionPool

    dsn = postgres_dsn_for_tests()
    ensure_migrated_and_clean(dsn)
    with ConnectionPool(dsn, min_size=1, max_size=4, timeout=5, open=True) as pool:
        yield pool


def _chain(pool, signer=None) -> LedgerChain:
    from nomos.level_1.ledger.postgres_storage import PostgresLedgerStorage

    storage = PostgresLedgerStorage(pool, CHAIN_ID, council_id="council-pg-test")
    return LedgerChain(storage=storage, signer=signer, chain_id=CHAIN_ID)


class TestPostgresLedgerRoundTrip:
    def test_initialize_append_get_head_iterate_validate(self, pool):
        signer = InMemorySigner(KeyPair.generate(key_id="council-pg-test"))
        chain = _chain(pool, signer)

        genesis = chain.initialize(creator_id="council-pg-test")
        assert genesis.sequence == 0
        assert genesis.previous_hash is None
        assert genesis.signatures

        first = chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-1"})
        second = chain.append(
            LedgerEntryType.INTENT_EVALUATED,
            {"intent_id": "i-1", "decision": "allow"},
            metadata={"writer": "test"},
        )

        assert first.sequence == 1
        assert second.sequence == 2
        assert second.previous_hash == first.content_hash
        assert chain.length() == 3
        assert chain.get_head().sequence == 2
        assert chain.get(1).payload == {"intent_id": "i-1"}
        assert [e.sequence for e in chain._storage.iterate()] == [0, 1, 2]
        assert chain.validate() is True
        # signatures round-trip and verify
        chain.register_verifier_key(signer.key_id, signer.public_key_bytes())
        assert chain.verify_signatures(second)

    def test_append_before_initialize_fails_closed(self, pool):
        chain = _chain(pool)
        with pytest.raises(LedgerAppendError, match="not initialized"):
            chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-x"})

    def test_query_helpers(self, pool):
        chain = _chain(pool)
        chain.initialize(creator_id="c")
        chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-1"})
        chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-2"})
        assert len(chain.query_by_type(LedgerEntryType.INTENT_SUBMITTED)) == 2
        assert len(chain.query_by_intent("i-2")) == 1


class TestIdempotentGenesis:
    def test_two_replicas_share_one_genesis(self, pool):
        replica_a = _chain(pool)
        replica_b = _chain(pool)

        genesis_a = replica_a.initialize(creator_id="replica-a")
        genesis_b = replica_b.initialize(creator_id="replica-b")

        assert genesis_a.content_hash == genesis_b.content_hash
        assert genesis_b.payload["creator_id"] == "replica-a"  # loser got winner's genesis
        assert replica_a.length() == 1
        # both replicas can append against the shared chain
        replica_a.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "a"})
        replica_b.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "b"})
        assert replica_b.validate() is True
        assert replica_a.length() == 3


class TestFailClosed:
    def test_lock_holdout_exhausts_retries_with_no_partial_rows(self, pool):
        import psycopg

        from nomos.level_1.ledger.postgres_storage import (
            LedgerRetryPolicy,
            PostgresLedgerStorage,
        )

        chain = _chain(pool)
        chain.initialize(creator_id="c")

        fast_retry = LedgerRetryPolicy(
            max_attempts=2, base_delay_s=0.01, max_delay_s=0.02, total_deadline_s=0.5
        )
        contender = LedgerChain(
            storage=PostgresLedgerStorage(
                pool,
                CHAIN_ID,
                council_id="council-pg-test",
                lock_timeout_ms=50,
                retry=fast_retry,
            ),
            chain_id=CHAIN_ID,
        )

        dsn = postgres_dsn_for_tests()
        with psycopg.connect(dsn) as blocker:
            blocker.execute(
                "SELECT 1 FROM ledger_chains WHERE chain_id = %s FOR UPDATE", (CHAIN_ID,)
            )
            # lock held in the open transaction; contender must fail closed
            with pytest.raises(LedgerAppendError):
                contender.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "blocked"})
            blocker.rollback()

        # no partial entry escaped a failed transaction
        assert chain.length() == 1
        assert chain.validate() is True


class TestAppendOnlyTriggers:
    """Migration 0005: the database itself refuses rewrites, deletes and head rollback."""

    @staticmethod
    def _seeded(pool) -> LedgerChain:
        signer = InMemorySigner(KeyPair.generate(key_id="council-pg-test"))
        chain = _chain(pool, signer)
        chain.initialize(creator_id="council-pg-test")
        chain.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "i-1"})
        chain.append(LedgerEntryType.INTENT_EVALUATED, {"intent_id": "i-1", "decision": "allow"})
        return chain

    @staticmethod
    def _rejected():
        from psycopg import errors

        return pytest.raises(errors.IntegrityConstraintViolation)

    def test_update_entry_rejected(self, pool):
        self._seeded(pool)
        with pool.connection() as conn, self._rejected():
            conn.execute(
                'UPDATE ledger_entries SET payload_json = \'{"intent_id": "FORGED"}\' '
                "WHERE chain_id = %s AND sequence = 1",
                (CHAIN_ID,),
            )

    def test_delete_entry_rejected(self, pool):
        self._seeded(pool)
        with pool.connection() as conn, self._rejected():
            conn.execute(
                "DELETE FROM ledger_entries WHERE chain_id = %s AND sequence = 2", (CHAIN_ID,)
            )

    def test_head_rollback_rejected(self, pool):
        chain = self._seeded(pool)
        first_hash = str(chain.get(1).content_hash)
        with pool.connection() as conn, self._rejected():
            conn.execute(
                "UPDATE ledger_chains SET current_head_sequence = 1, current_head_hash = %s "
                "WHERE chain_id = %s",
                (first_hash, CHAIN_ID),
            )

    def test_head_skip_and_dangling_hash_rejected(self, pool):
        chain = self._seeded(pool)
        head_hash = str(chain.get_head().content_hash)
        with pool.connection() as conn, self._rejected():
            conn.execute(
                "UPDATE ledger_chains SET current_head_sequence = 4, current_head_hash = %s "
                "WHERE chain_id = %s",
                (head_hash, CHAIN_ID),
            )
        with pool.connection() as conn, self._rejected():
            # Advancing by one but to a hash that no entry has.
            conn.execute(
                "UPDATE ledger_chains SET current_head_sequence = 3, "
                "current_head_hash = 'sha256:ffff' WHERE chain_id = %s",
                (CHAIN_ID,),
            )

    def test_chain_delete_and_identity_change_rejected(self, pool):
        self._seeded(pool)
        with pool.connection() as conn, self._rejected():
            conn.execute("DELETE FROM ledger_chains WHERE chain_id = %s", (CHAIN_ID,))
        with pool.connection() as conn, self._rejected():
            conn.execute(
                "UPDATE ledger_chains SET council_id = 'other' WHERE chain_id = %s", (CHAIN_ID,)
            )

    def test_null_head_cannot_jump_past_genesis(self, pool):
        """NULL -> anything but 0 is rejected (NULL + 1 must not fall through)."""
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO ledger_chains (chain_id, council_id, created_by) "
                "VALUES ('ledger-empty', 'council-pg-test', 'test')"
            )
        with pool.connection() as conn, self._rejected():
            conn.execute(
                "UPDATE ledger_chains SET current_head_sequence = 5, "
                "current_head_hash = 'sha256:ffff' WHERE chain_id = 'ledger-empty'"
            )

    def test_chain_insert_with_head_rejected(self, pool):
        with pool.connection() as conn, self._rejected():
            conn.execute(
                "INSERT INTO ledger_chains (chain_id, council_id, created_by, "
                "current_head_sequence, current_head_hash) "
                "VALUES ('ledger-forged', 'council-pg-test', 'test', 0, 'sha256:ffff')"
            )

    def test_entry_insert_off_the_head_rejected(self, pool):
        chain = self._seeded(pool)  # head is 2
        head_hash = str(chain.get_head().content_hash)
        insert = (
            "INSERT INTO ledger_entries (chain_id, sequence, entry_type, payload_json, "
            "previous_hash, entry_hash, writer_instance_id, entry_timestamp_iso) "
            "VALUES (%s, %s, 'intent_submitted', '{}', %s, 'sha256:eeee', 'test', "
            "'2026-01-01T00:00:00+00:00')"
        )
        with pool.connection() as conn, self._rejected():
            conn.execute(insert, (CHAIN_ID, 1, head_hash))  # behind the head
        with pool.connection() as conn, self._rejected():
            conn.execute(insert, (CHAIN_ID, 4, head_hash))  # skips a sequence
        with pool.connection() as conn, self._rejected():
            conn.execute(insert, (CHAIN_ID, 3, "sha256:ffff"))  # next, but unlinked
        with pool.connection() as conn, self._rejected():
            conn.execute(insert, ("ledger-nonexistent", 0, None))
        assert chain.append(LedgerEntryType.DECISION_MADE, {"intent_id": "i-1"}).sequence == 3

    def test_status_change_and_normal_append_still_work(self, pool):
        chain = self._seeded(pool)
        with pool.connection() as conn:
            conn.execute(
                "UPDATE ledger_chains SET status = 'archived' WHERE chain_id = %s", (CHAIN_ID,)
            )
        entry = chain.append(LedgerEntryType.DECISION_MADE, {"intent_id": "i-1"})
        assert entry.sequence == 3
        assert chain.get_head().sequence == 3
        assert chain.validate() is True
        assert chain.status().signatures_verified is True


@pytest.fixture(scope="module")
def separated_ledger_roles(request, tmp_path_factory):
    """A real migration owner and runtime with DML but no table ownership/DDL."""
    import uuid

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from nomos.store.migrate import DEFAULT_MIGRATIONS_DIR, run_migrations

    migrations_dir = DEFAULT_MIGRATIONS_DIR
    if getattr(request, "param", None) == "0005":
        migrations_dir = tmp_path_factory.mktemp("ledger-through-0005")
        for path in DEFAULT_MIGRATIONS_DIR.glob("*.sql"):
            if path.name < "0006":
                (migrations_dir / path.name).write_text(path.read_text())

    suffix = uuid.uuid4().hex[:12]
    owner = f"ledger_owner_{suffix}"
    runtime = f"ledger_runtime_{suffix}"
    schema = f"ledger_roles_{suffix}"
    admin_dsn = postgres_dsn_for_tests()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        can_create_roles = admin.execute(
            "SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]
        if not can_create_roles:
            pytest.skip("runtime privilege tests require a disposable CREATEROLE test database")
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(owner)))
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(runtime)))
        try:
            admin.execute(
                sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    sql.Identifier(schema), sql.Identifier(owner)
                )
            )
            owner_dsn = make_conninfo(admin_dsn, user=owner, options=f"-c search_path={schema}")
            runtime_dsn = make_conninfo(admin_dsn, user=runtime, options=f"-c search_path={schema}")
            applied = run_migrations(owner_dsn, migrations_dir=migrations_dir)
            assert "0005_ledger_append_only" in applied
            assert run_migrations(owner_dsn, migrations_dir=migrations_dir) == []
            with psycopg.connect(owner_dsn) as conn:
                conn.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema), sql.Identifier(runtime)
                    )
                )
                conn.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ledger_chains, "
                        "ledger_entries TO {}"
                    ).format(sql.Identifier(runtime))
                )
                assert conn.execute("SELECT current_user").fetchone()[0] == owner
            yield owner_dsn, runtime_dsn
        finally:
            for role in (runtime, owner):
                admin.execute(sql.SQL("DROP OWNED BY {} CASCADE").format(sql.Identifier(role)))
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.fixture
def runtime_ledger_pool(separated_ledger_roles):
    import psycopg
    from psycopg_pool import ConnectionPool

    owner_dsn, runtime_dsn = separated_ledger_roles
    with psycopg.connect(owner_dsn) as conn:
        conn.execute("TRUNCATE ledger_entries, ledger_chains CASCADE")
    with ConnectionPool(runtime_dsn, min_size=1, max_size=2, timeout=5, open=True) as pool:
        yield pool


class TestRestrictedRuntimeLedger:
    """Runtime enforcement only: owners and superusers can change these triggers."""

    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE ledger_entries SET payload_json = '{}' WHERE sequence = 1",
            "DELETE FROM ledger_entries WHERE sequence = 1",
            "DELETE FROM ledger_chains",
            "UPDATE ledger_chains SET current_head_sequence = 1",
            "UPDATE ledger_chains SET current_head_sequence = NULL, current_head_hash = NULL",
            "UPDATE ledger_chains SET current_head_sequence = 4",
            "UPDATE ledger_chains SET current_head_sequence = 3, current_head_hash = 'sha256:bad'",
            "UPDATE ledger_chains SET chain_id = 'rewritten'",
            "UPDATE ledger_chains SET council_id = 'rewritten'",
            "UPDATE ledger_chains SET created_by = 'rewritten'",
            "UPDATE ledger_chains SET created_at = now()",
            "INSERT INTO ledger_chains (chain_id, council_id, created_by, "
            "current_head_sequence, current_head_hash) VALUES ('forged', 'test', 'test', 0, 'bad')",
            "INSERT INTO ledger_entries (chain_id, sequence, entry_type, payload_json, "
            "previous_hash, entry_hash, writer_instance_id, entry_timestamp_iso) "
            "VALUES ('ledger-nonexistent', 0, 'intent_submitted', '{}', "
            "NULL, 'bad', 'test', 'test')",
            "INSERT INTO ledger_entries (chain_id, sequence, entry_type, payload_json, "
            "previous_hash, entry_hash, writer_instance_id, entry_timestamp_iso) "
            "SELECT chain_id, 4, 'intent_submitted', '{}', current_head_hash, "
            "'bad', 'test', 'test' "
            "FROM ledger_chains",
        ],
    )
    def test_dml_rewrites_fail(self, runtime_ledger_pool, statement):
        from psycopg import errors

        chain = TestAppendOnlyTriggers._seeded(runtime_ledger_pool)
        with (
            runtime_ledger_pool.connection() as conn,
            pytest.raises(errors.IntegrityConstraintViolation),
        ):
            conn.execute(statement)
        assert chain.get_head().sequence == 2
        assert chain.validate() is True

    @pytest.mark.parametrize(
        "statement",
        [
            "TRUNCATE ledger_entries",
            "TRUNCATE ledger_entries, ledger_chains CASCADE",
            "ALTER TABLE ledger_entries DISABLE TRIGGER ALL",
            "ALTER TABLE ledger_chains DISABLE TRIGGER ALL",
            "DROP TRIGGER ledger_entries_append_only ON ledger_entries",
            "DROP TRIGGER ledger_chains_head_monotonic ON ledger_chains",
            "DROP FUNCTION ledger_entries_append_only() CASCADE",
            "DROP TABLE ledger_entries CASCADE",
        ],
    )
    def test_runtime_cannot_remove_enforcement(self, runtime_ledger_pool, statement):
        from psycopg import errors

        TestAppendOnlyTriggers._seeded(runtime_ledger_pool)
        with runtime_ledger_pool.connection() as conn, pytest.raises(errors.InsufficientPrivilege):
            conn.execute(statement)

    def test_runtime_normal_append_and_matching_head_advance(self, runtime_ledger_pool):
        chain = TestAppendOnlyTriggers._seeded(runtime_ledger_pool)
        entry = chain.append(LedgerEntryType.DECISION_MADE, {"intent_id": "i-1"})
        assert entry.sequence == 3
        with runtime_ledger_pool.connection() as conn:
            row = conn.execute(
                "SELECT current_head_sequence, current_head_hash FROM ledger_chains "
                "WHERE chain_id = %s",
                (CHAIN_ID,),
            ).fetchone()
            assert row == (3, str(entry.content_hash))
            assert conn.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
            ).fetchone() == (False,)
            conn.execute("UPDATE ledger_chains SET description = 'allowed metadata edit'")
        assert chain.validate() is True
        assert chain.status().signatures_verified is True


@pytest.mark.parametrize("previous_hash", ["sha256:unlinked", ""])
def test_runtime_rejects_non_null_genesis(runtime_ledger_pool, previous_hash):
    from psycopg import errors

    with runtime_ledger_pool.connection() as conn:
        conn.execute(
            "INSERT INTO ledger_chains (chain_id, council_id, created_by) "
            "VALUES (%s, 'test', 'test')",
            (CHAIN_ID,),
        )
        assert conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone() == (False,)
    with (
        runtime_ledger_pool.connection() as conn,
        pytest.raises(errors.IntegrityConstraintViolation),
    ):
        _insert_raw_genesis(conn, CHAIN_ID, previous_hash)
    with runtime_ledger_pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM ledger_entries").fetchone() == (0,)
        assert conn.execute(
            "SELECT current_head_sequence, current_head_hash FROM ledger_chains"
        ).fetchone() == (None, None)
    # A refused genesis does not prevent a valid signed genesis and +1 append.
    chain = TestAppendOnlyTriggers._seeded(runtime_ledger_pool)
    assert chain.status().is_valid is True
    assert chain.status().signatures_verified is True


def _insert_raw_genesis(conn, chain_id, previous_hash):
    conn.execute(
        "INSERT INTO ledger_entries (chain_id, sequence, entry_type, payload_json, "
        "previous_hash, entry_hash, writer_instance_id, entry_timestamp_iso) "
        "VALUES (%s, 0, 'policy_updated', '{}', %s, 'sha256:genesis', 'test', "
        "'2026-01-01T00:00:00+00:00')",
        (chain_id, previous_hash),
    )
    conn.execute(
        "UPDATE ledger_chains SET current_head_sequence = 0, "
        "current_head_hash = 'sha256:genesis' WHERE chain_id = %s",
        (chain_id,),
    )


@pytest.mark.parametrize("separated_ledger_roles", ["0005"], indirect=True)
def test_genesis_guard_upgrades_existing_0005_without_repair(separated_ledger_roles):
    """Upgrade preserves historical invalid evidence and protects new inserts."""
    import psycopg
    from psycopg import errors
    from psycopg_pool import ConnectionPool

    from nomos.level_0.crypto import ContentHash
    from nomos.level_1.ledger.postgres_storage import PostgresLedgerStorage
    from nomos.store.migrate import DEFAULT_MIGRATIONS_DIR, run_migrations

    owner_dsn, runtime_dsn = separated_ledger_roles
    # This is the actual old function installed through the normal runner.
    # Store a correctly hashed and signed, but unlinked, historical genesis.
    signer = InMemorySigner(KeyPair.generate(key_id="upgrade-test"))
    with ConnectionPool(runtime_dsn, min_size=1, max_size=2, timeout=5) as pool:
        chain = _chain(pool, signer)
        genesis = chain._build_genesis("upgrade-test")
        genesis.previous_hash = ContentHash.from_string("sha256:" + "ab" * 32)
        genesis.content_hash = chain._hasher.hash_json(genesis.content_for_hashing())
        genesis.signatures = [signer.sign(genesis.content_hash.to_bytes())]
        storage = PostgresLedgerStorage(pool, CHAIN_ID, council_id="council-pg-test")
        storage.initialize_atomic(lambda: genesis)
        assert chain.verify_signatures(genesis) is True
        assert chain.status().is_valid is False
        existing_good = LedgerChain(
            storage=PostgresLedgerStorage(pool, "valid-before-upgrade", council_id="test"),
            signer=signer,
            chain_id="valid-before-upgrade",
        )
        existing_good.initialize(creator_id="test")
        existing_good.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "old-valid"})
        assert existing_good.status().signatures_verified is True
        with psycopg.connect(owner_dsn) as conn:
            before = conn.execute(
                "SELECT * FROM ledger_entries ORDER BY chain_id, sequence"
            ).fetchall()
            heads_before = conn.execute("SELECT * FROM ledger_chains ORDER BY chain_id").fetchall()
        assert run_migrations(owner_dsn) == ["0006_ledger_genesis_guard"]
        assert run_migrations(owner_dsn) == []
        # The SQL replacement itself is idempotent as well as the runner.
        with psycopg.connect(owner_dsn) as conn:
            conn.execute((DEFAULT_MIGRATIONS_DIR / "0006_ledger_genesis_guard.sql").read_text())
            assert (
                conn.execute("SELECT * FROM ledger_entries ORDER BY chain_id, sequence").fetchall()
                == before
            )
            assert (
                conn.execute("SELECT * FROM ledger_chains ORDER BY chain_id").fetchall()
                == heads_before
            )
        assert (
            existing_good.append(
                LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "still-valid"}
            ).sequence
            == 2
        )
        assert existing_good.status().signatures_verified is True
        assert chain.status().is_valid is False
        assert chain.status().signatures_verified is False
        with psycopg.connect(runtime_dsn) as conn:
            conn.execute(
                "INSERT INTO ledger_chains (chain_id, council_id, created_by) "
                "VALUES ('after-upgrade', 'test', 'test')"
            )
        with (
            psycopg.connect(runtime_dsn) as conn,
            pytest.raises(errors.IntegrityConstraintViolation),
        ):
            _insert_raw_genesis(conn, "after-upgrade", "sha256:unlinked")
        good = LedgerChain(
            storage=PostgresLedgerStorage(pool, "after-upgrade", council_id="test"),
            signer=signer,
            chain_id="after-upgrade",
        )
        assert good.initialize(creator_id="test").previous_hash is None
        assert good.append(LedgerEntryType.INTENT_SUBMITTED, {"intent_id": "valid"}).sequence == 1
        assert good.status().is_valid is True
        assert good.status().signatures_verified is True
