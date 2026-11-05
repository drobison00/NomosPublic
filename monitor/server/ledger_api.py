# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Council-API ledger source for the monitor.

In PVC-free deployments the monitor holds no database credentials and mounts
no ledger volume: it reads ledger entries through the council's
`council_ledger_page` / `council_ledger_get` tools (NAT debug endpoint) and
renders them through the same view/verification layer as file mode
(hash re-verification, link checks, redaction).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

import httpx

from monitor.server.ledger import (
    DEFAULT_PAGE_LIMIT,
    FILTER_SCAN_LIMIT,
    MAX_PAGE_LIMIT,
    LedgerChainView,
    LedgerEntryView,
    _decode_cursor,
    _encode_cursor,
    _entry_from_raw,
    _entry_matches,
)

logger = logging.getLogger(__name__)

# The council serves at most this many entries per page.
COUNCIL_PAGE_CAP = 200


async def _call_tool(
    client: httpx.AsyncClient,
    api_url: str,
    tool: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Invoke a council tool via the NAT debug endpoint and decode its result."""
    url = f"{api_url.rstrip('/')}/debug/tools/call/{tool}"
    response = await client.post(url, json=payload, timeout=10.0)
    response.raise_for_status()
    body = response.json()
    if isinstance(body, str):
        body = json.loads(body)
    result = body.get("result", body)
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        raise ValueError(f"unexpected {tool} response shape")
    return result


def _views_from_page(
    raw_entries: list[dict[str, Any]],
    chain_id: str,
    public_keys: list[bytes] | None = None,
    *,
    newer_than_first: dict[str, Any] | None = None,
) -> list[LedgerEntryView]:
    """Build entry views from a newest-first page, chaining link expectations.

    Entry N's content hash must equal entry N+1's previous_hash; within one
    contiguous newest-first page the successor of entry i is element i-1.
    ``newer_than_first`` is the raw entry that precedes this page (the last
    entry of the newer page), so a link that straddles a page boundary is
    checked too.
    """
    views: list[LedgerEntryView] = []
    for index, parsed in enumerate(raw_entries):
        expected_previous: str | None = None
        newer = raw_entries[index - 1] if index > 0 else newer_than_first
        if newer is not None and newer.get("sequence") == (parsed.get("sequence") or 0) + 1:
            expected_previous = str(newer.get("previous_hash") or "")
        views.append(
            _entry_from_raw(
                parsed,
                chain_id=chain_id,
                expected_previous=expected_previous,
                public_keys=public_keys,
            )
        )
    return views


def _chain_view(
    chain_id: str, head_sequence: int, head_view: LedgerEntryView | None
) -> LedgerChainView:
    entry_count = max(0, head_sequence + 1)
    signed_count = entry_count if head_view and head_view.signature_count else 0
    # Fast mode only inspects the head; a head whose signature fails against
    # the trusted keys is as broken as a head whose hash does not match.
    head_ok = head_view is None or (head_view.hash_valid and head_view.signature_valid is not False)
    summary_entries: tuple[LedgerEntryView, ...] = ()
    if head_view is not None:
        summary_entries = (
            LedgerEntryView(
                **{
                    **head_view.__dict__,
                    "source": "council-api",
                    "sequence": entry_count - 1,
                    "entry_type": "__summary__",
                    "payload": {"entry_count": entry_count},
                    "metadata": {"signed_count": signed_count},
                }
            ),
        )
    return LedgerChainView(
        chain_id=chain_id,
        source="council-api",
        entries=summary_entries,
        is_valid=head_ok,
        head_hash=head_view.content_hash if head_view else "",
        head_sequence=head_sequence,
        broken_at=None if head_ok else head_sequence,
    )


def _fold_page_failures(chain: LedgerChainView, views: list[LedgerEntryView]) -> LedgerChainView:
    """Carry a failure observed anywhere in the fetched page into the chain summary.

    The summary is built from the head alone, but the page below it has already
    been verified; an older entry whose hash, link or signature fails must not
    sit under a chain still reported as valid.
    """
    failed = [
        view.sequence
        for view in views
        if not view.hash_valid or not view.link_valid or view.signature_valid is False
    ]
    if not failed:
        return chain
    broken_at = min(failed)
    if chain.broken_at is not None:
        broken_at = min(broken_at, chain.broken_at)
    return replace(chain, is_valid=False, broken_at=broken_at)


async def _validate_whole_chain(
    client: httpx.AsyncClient,
    api_url: str,
    chain: LedgerChainView,
    head_sequence: int,
    *,
    public_keys: list[bytes] | None,
) -> LedgerChainView:
    """Walk every page from the head to sequence 0 and fold each failure in.

    The display page is capped, so ``validate=True`` must not stop there: an
    older forged or unsigned entry has to reach the chain summary even when it
    sits beyond the first page. Display pagination is left untouched.
    """
    signatures_verified = bool(public_keys)
    expected = head_sequence
    newer: dict[str, Any] | None = None  # last entry of the previous (newer) page
    while expected >= 0:
        # Every successful request consumes at least one expected sequence, so
        # the entire walk is bounded by head_sequence + 1 requests.
        cursor = expected
        requested_limit = min(COUNCIL_PAGE_CAP, expected + 1)
        page = await _call_tool(
            client, api_url, "council_ledger_page", {"cursor": cursor, "limit": requested_limit}
        )
        if page.get("error"):
            raise ValueError(str(page["error"]))
        if page.get("chain_id") != chain.chain_id:
            raise ValueError("ledger chain changed during validation")
        raw_entries = page.get("entries")
        if not isinstance(raw_entries, list) or not 1 <= len(raw_entries) <= requested_limit:
            raise ValueError("invalid ledger validation page size")
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise ValueError("invalid ledger entry")
            sequence = raw.get("sequence")
            if type(sequence) is not int or sequence != expected or expected < 0:
                raise ValueError("ledger validation requires contiguous descending sequences")
            expected -= 1
        if newer is None and raw_entries[0].get("content_hash") != chain.head_hash:
            raise ValueError("ledger head changed during validation")
        views = _views_from_page(raw_entries, chain.chain_id, public_keys, newer_than_first=newer)
        chain = _fold_page_failures(chain, views)
        newer = raw_entries[-1]
        if any(view.signature_valid is not True for view in views):
            signatures_verified = False
        raw_next = page.get("next_cursor")
        if expected >= 0:
            if type(raw_next) is not int or raw_next != expected or raw_next >= cursor:
                raise ValueError("ledger cursor does not match the next expected sequence")
        elif raw_next is not None:
            raise ValueError("ledger cursor continues past genesis")
    return replace(chain, fully_validated=True, signatures_verified=signatures_verified)


async def load_ledger_page_from_api(
    client: httpx.AsyncClient,
    api_url: str,
    *,
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str = "",
    chain_id: str = "",
    entry_type: str = "",
    query: str = "",
    integrity: str = "",
    validate: bool = False,
    public_keys: list[bytes] | None = None,
) -> dict[str, Any]:
    """Load one ledger page through the council API (same shape as file mode)."""
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    fetch_limit = FILTER_SCAN_LIMIT if any((entry_type, query, integrity)) else limit
    fetch_limit = min(fetch_limit, COUNCIL_PAGE_CAP)

    try:
        before = _decode_cursor(cursor)
        # Resolve the chain head first so cursors stay consistent.
        head_page = await _call_tool(
            client, api_url, "council_ledger_page", {"cursor": None, "limit": 1}
        )
        if head_page.get("error"):
            raise ValueError(str(head_page["error"]))
        council_chain_id = str(head_page.get("chain_id") or "")
        head_sequence = head_page.get("head_sequence")
        if type(head_sequence) is not int or head_sequence < -1:
            raise ValueError("invalid ledger head sequence")
        head_entries = head_page.get("entries")
        if not isinstance(head_entries, list) or len(head_entries) != int(head_sequence >= 0):
            raise ValueError("ledger head does not match its entries")
        head_raw = head_entries[0] if head_entries else None
        if head_raw is not None and (
            not isinstance(head_raw, dict)
            or type(head_raw.get("sequence")) is not int
            or head_raw["sequence"] != head_sequence
        ):
            raise ValueError("invalid ledger head entry")

        if chain_id and council_chain_id != chain_id:
            return _empty_page(limit, validate, error="")

        head_view = (
            _entry_from_raw(
                head_raw,
                chain_id=council_chain_id,
                expected_previous=None,
                public_keys=public_keys,
            )
            if head_raw
            else None
        )
        chain = _chain_view(council_chain_id, head_sequence, head_view)

        before_seq = before.get(council_chain_id, head_sequence + 1)
        page_views: list[LedgerEntryView] = []
        next_cursor_seq: int | None = None
        if head_sequence >= 0 and before_seq > 0:
            page = await _call_tool(
                client,
                api_url,
                "council_ledger_page",
                {"cursor": before_seq - 1, "limit": fetch_limit},
            )
            if page.get("error"):
                raise ValueError(str(page["error"]))
            raw_entries = list(page.get("entries") or [])
            views = _views_from_page(raw_entries, council_chain_id, public_keys)
            chain = _fold_page_failures(chain, views)
            for view in views:
                if _entry_matches(view, entry_type=entry_type, query=query, integrity=integrity):
                    page_views.append(view)
                    if len(page_views) >= limit:
                        break
            raw_next = page.get("next_cursor")
            if page_views:
                next_cursor_seq = page_views[-1].sequence
            elif raw_next is not None:
                next_cursor_seq = int(raw_next) + 1

        if validate:
            if head_sequence >= 0:
                chain = await _validate_whole_chain(
                    client, api_url, chain, head_sequence, public_keys=public_keys
                )
            else:
                chain = replace(chain, fully_validated=True)

        has_more = bool(page_views) and (next_cursor_seq or 0) > 0
        next_before = dict(before)
        if next_cursor_seq is not None:
            next_before[council_chain_id] = next_cursor_seq

        entry_count = max(0, head_sequence + 1)
        signed = chain.summary().get("signed_count", 0)
        return {
            "chains": [{**chain.summary(), "fully_validated": chain.fully_validated}],
            "entries": [view.summary() for view in page_views],
            "next_cursor": _encode_cursor(next_before) if has_more else "",
            "has_more": has_more,
            "page_limit": limit,
            "validation_mode": "full" if validate else "fast",
            "summary": {
                "chain_count": 1,
                "entry_count": entry_count,
                "valid_chain_count": 1 if chain.is_valid else 0,
                "invalid_chain_count": 0 if chain.is_valid else 1,
                "signed_entry_count": signed,
                "unsigned_entry_count": max(0, entry_count - signed),
            },
        }
    except Exception as exc:
        logger.warning("council ledger API unavailable: %s", exc)
        return _empty_page(limit, validate, error="council ledger API unavailable")


async def get_ledger_entry_from_api(
    client: httpx.AsyncClient,
    api_url: str,
    *,
    chain_id: str,
    sequence: int,
    public_keys: list[bytes] | None = None,
) -> dict[str, Any] | None:
    """Fetch one entry (with its successor for link verification) via the API."""
    try:
        page = await _call_tool(
            client,
            api_url,
            "council_ledger_page",
            {"cursor": sequence + 1, "limit": 2},
        )
        if page.get("error"):
            return None
        council_chain_id = str(page.get("chain_id") or "")
        if chain_id and council_chain_id != chain_id:
            return None
        raw_entries = list(page.get("entries") or [])
        views = _views_from_page(raw_entries, council_chain_id, public_keys)
        for view in views:
            if view.sequence == sequence:
                return view.detail()
        return None
    except Exception as exc:
        logger.warning("council ledger API unavailable: %s", exc)
        return None


def _empty_page(limit: int, validate: bool, *, error: str) -> dict[str, Any]:
    page: dict[str, Any] = {
        "chains": [],
        "entries": [],
        "next_cursor": "",
        "has_more": False,
        "page_limit": limit,
        "validation_mode": "full" if validate else "fast",
        "summary": {
            "chain_count": 0,
            "entry_count": 0,
            "valid_chain_count": 0,
            "invalid_chain_count": 0,
            "signed_entry_count": 0,
            "unsigned_entry_count": 0,
        },
    }
    if error:
        page["error"] = error
    return page
