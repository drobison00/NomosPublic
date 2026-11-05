-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
-- SPDX-License-Identifier: Apache-2.0

-- Upgrade the existing 0005 INSERT trigger in place. Genesis must link to
-- nothing, including when the runtime directly inserts and advances the head.
-- CREATE OR REPLACE preserves the trigger binding and is safe to repeat.
-- No historical rows are changed or deleted: an already stored malformed
-- genesis remains invalid and must still be reported as such by validation.
-- This enforces runtime DML; owners/superusers can alter trigger enforcement.

CREATE OR REPLACE FUNCTION ledger_entries_next_only() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    head_sequence bigint;
    head_hash text;
BEGIN
    SELECT current_head_sequence, current_head_hash INTO head_sequence, head_hash
      FROM ledger_chains WHERE chain_id = NEW.chain_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ledger_entries insert for unknown chain %', NEW.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.sequence IS DISTINCT FROM COALESCE(head_sequence + 1, 0) THEN
        RAISE EXCEPTION
            'ledger_entries insert must be the next sequence (% expected, % rejected on %)',
            COALESCE(head_sequence + 1, 0), NEW.sequence, NEW.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF head_sequence IS NULL AND NEW.previous_hash IS NOT NULL THEN
        RAISE EXCEPTION 'ledger_entries genesis must have no previous hash (%)', NEW.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF head_sequence IS NOT NULL AND NEW.previous_hash IS DISTINCT FROM head_hash THEN
        RAISE EXCEPTION
            'ledger_entries insert % of % does not link to the current head',
            NEW.sequence, NEW.chain_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;
