All eight cited code references are confirmed in the repository. The logic holds on 64-bit platforms: `u32::MAX` (4,294,967,295) < `usize::MAX` (2⁶⁴−1), so the guard at `get_cells.rs:29` never fires when `request_limit` is `usize::MAX`. The `instr()` path, `fetch_all` connection hold, pool cap, and shared pool between RPC and sync are all verified.

---

Audit Report

## Title
Connection Pool Exhaustion via Unbounded `get_cells` with `Partial` Script Search Mode — (`util/rich-indexer/src/`)

## Summary
When `request_limit` is absent from config it defaults to `usize::MAX`, making the per-request limit guard a no-op for any `u32` input. An unprivileged caller can submit 10 concurrent `get_cells` requests with `limit=0xffffffff` and `script_search_mode=Partial`, each triggering a full-table `instr(args, $1) > 0` scan on SQLite while holding a pool connection via `fetch_all` for the entire scan duration. With the pool capped at 10 connections and a 60-second acquire timeout, all connections are exhausted, causing every subsequent indexer RPC call and every `append`/`rollback` sync operation to block for 60 seconds before failing.

## Finding Description
**Ineffective limit guard:**
`RichIndexerService::new` sets `request_limit: config.request_limit.unwrap_or(usize::MAX)` (`service.rs:51`). The guard `if limit as usize > self.request_limit` (`get_cells.rs:29`) compares a `u32`-derived `usize` against `usize::MAX`; on any 64-bit platform `u32::MAX` (4,294,967,295) is strictly less than `usize::MAX` (2⁶⁴−1), so the check never fires. The raw value is forwarded unchanged to `query_builder.limit(limit)` (`get_cells.rs:156`).

**Full-table scan via `Partial` mode:**
For SQLite, `Partial` mode emits `instr(args, $1) > 0` (`mod.rs:110`). The `instr()` function cannot use any B-tree index on the `args` column; SQLite must perform a sequential scan of the entire `script` table to find matching rows.

**Connection held for full scan duration:**
`fetch_all` acquires a connection from the pool and materialises all matching rows before returning (`store.rs:143-148`). With `limit=u32::MAX` and a full-table `instr()` scan, the connection is held for the entire (unbounded) scan duration.

**Shared pool exhaustion blocks sync:**
`SQLXPool` wraps `Arc<OnceLock<AnyPool>>` (`store.rs:29-31`) and is cloned into both the RPC handle and the indexer sync path. `AsyncRichIndexer::append` calls `self.store.transaction()` (`indexer/mod.rs:156-161`), which acquires from the same 10-connection pool (`store.rs:46-50`). Ten concurrent attacker requests exhaust all connections; every subsequent RPC call and every sync operation blocks for 60 seconds before receiving a pool-timeout error.

## Impact Explanation
The impact is a repeatable, externally-triggered denial of service against the rich-indexer RPC surface: all indexer RPC methods (`get_cells`, `get_transactions`, `get_cells_capacity`, `get_indexer_tip`) become unresponsive for 60-second windows, and indexer sync stalls for the same duration. This matches **Note (0–500 points) — Any local RPC API crash**, as the indexer RPC is rendered non-functional for the duration of each attack wave. The core CKB consensus and p2p layers are unaffected; the impact is confined to the optional rich-indexer component and its RPC endpoints.

## Likelihood Explanation
The default configuration (no `request_limit` set) is the common deployment case. The RPC endpoint is routinely exposed on indexer nodes. The attack requires only 10 standard JSON-RPC calls with no authentication, no proof-of-work, and no privileged access. It is fully repeatable: as soon as one batch of connections times out or completes, the attacker fires another 10 requests.

## Recommendation
1. Replace `usize::MAX` with a safe default for `request_limit` (e.g., `100` or `1000`).
2. Add a hard upper bound on `limit` independent of `request_limit` (e.g., cap at `u16::MAX` or a fixed constant) so a misconfigured or absent `request_limit` cannot be exploited.
3. Apply a query-level timeout at the SQLx layer so long-running scans release their connections before the pool acquire timeout fires.
4. Consider a separate, smaller connection pool for RPC read queries versus indexer-sync write transactions to prevent cross-contamination.

## Proof of Concept
```bash
# CKB with rich-indexer, SQLite backend, no request_limit in config.
for i in $(seq 1 10); do
  curl -s -X POST http://localhost:8116 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"get_cells","params":[
          {"script":{"code_hash":"0x0000000000000000000000000000000000000000000000000000000000000000",
           "hash_type":"data","args":"0x01"},
           "script_type":"lock","script_search_mode":"partial"},
          "asc","0xffffffff",null],"id":'$i'}' &
done
# 11th call blocks for >=60s:
time curl -s -X POST http://localhost:8116 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_indexer_tip","params":[],"id":11}'
# Expected: response delayed ~60s; indexer block_number does not advance during window.
```