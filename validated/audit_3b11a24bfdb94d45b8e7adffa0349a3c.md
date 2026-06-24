Audit Report

## Title
Unbounded O(N × log H) DB Read Amplification in `get_block_numbers_via_difficulties` — (`util/light-client-protocol-server/src/components/get_last_state_proof.rs`)

## Summary

The `GetLastStateProof` handler allows an unprivileged remote peer to supply up to 1000 difficulty values that each trigger an independent binary search over the full block range, costing O(log H) DB reads per entry. The combined limit check, monotonicity check, and skip guard are all individually insufficient to bound the total DB work per request, enabling sustained IO amplification with no rate limiting on this path.

## Finding Description

**Limit check** — Line 201 enforces:
```rust
if self.message.difficulties().len() + (last_n_blocks as usize) * 2
    > constant::GET_LAST_STATE_PROOF_LIMIT   // = 1000
```
With `last_n_blocks = 0`, an attacker may supply exactly 1000 difficulty entries. `GET_LAST_STATE_PROOF_LIMIT` is confirmed as 1000 in `util/light-client-protocol-server/src/constant.rs` line 6.

**Monotonicity check** — Line 254 enforces strict increase:
```rust
if difficulties.windows(2).any(|d| d[0] >= d[1]) { … reject … }
```
This prevents duplicates but does not prevent 1000 distinct values that all map to blocks near the chain's genesis.

**Binary search per entry** — `get_block_numbers_via_difficulties` (lines 73–103) iterates over every difficulty and calls `get_first_block_total_difficulty_is_not_less_than`, which is a full binary search over `[start_block_number, end_block_number)`. After finding block `num`, the next search starts from `num - 1` (lines 90–91):
```rust
if num > start_block_number {
    start_block_number = num - 1;
}
```
If all 1000 difficulties map to blocks 1, 2, …, 1000 on a 1M-block chain, each subsequent search starts from `i − 1` and ends at `difficulty_boundary_block_number` (~1M), covering nearly the full range. Each binary search costs O(log H) ≈ 20 iterations.

**DB reads per step** — Each iteration of the binary search calls `get_block_total_difficulty` (lines 111–116), which performs two DB lookups: `get_block_hash` + `get_block_ext`.

**Skip guard is bypassable** — The `current_difficulty >= *difficulty` check at line 82 only fires when the previously found block's total difficulty already exceeds the next requested difficulty. An attacker who sets each `d[i]` to the exact total difficulty of block `i` ensures `current_difficulty == d[i-1] < d[i]`, so the skip never triggers.

**No rate limiting** — `lib.rs` lines 108–112 show `GetLastStateProof` is dispatched directly to `execute()` with no per-peer request throttling. A ban only occurs for malformed messages or explicitly invalid requests; a valid but maximally expensive request is never banned.

## Impact Explanation

On a 1M-block chain: 1000 difficulties × ~20 binary search iterations × 2 DB reads = **~40,000 DB reads per single request**. A single persistent peer sending back-to-back crafted messages can saturate the node's storage IO, degrading its ability to serve other peers and process new blocks. This matches **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**, and potentially **High: bad designs which could cause CKB network congestion with few costs**, since the attacker cost is trivial (read 1000 block difficulties, craft a message, loop).

## Likelihood Explanation

- Any unprivileged peer can send `GetLastStateProof` messages.
- All required information (block total difficulties) is publicly readable from the chain.
- The attack is trivially scriptable: read difficulties at blocks 1…1000, send the message in a tight loop.
- No authentication, no per-peer rate limiting, and no ban is triggered by this pattern.

## Recommendation

1. **Cap total binary search work.** Track the cumulative number of `get_block_total_difficulty` calls across the entire `get_block_numbers_via_difficulties` invocation and abort once a budget (e.g., `2 × GET_LAST_STATE_PROOF_LIMIT`) is exceeded.
2. **Enforce a separate cap on `difficulties.len()`** independent of `last_n_blocks`, so the combined formula cannot allow 1000 difficulty entries when `last_n_blocks = 0`.
3. **Add per-peer request rate limiting** for `GetLastStateProof` messages in the `received` handler in `lib.rs`.

## Proof of Concept

```python
chain_height = 1_000_000
# Read exact total_difficulty at blocks 1..1000 from the chain (public data)
difficulties = [total_difficulty(i) for i in range(1, 1001)]
# Strictly increasing (valid chain property), passes monotonicity check
# len = 1000, last_n_blocks = 0 → passes limit check (1000 + 0 = 1000, not > 1000)

msg = GetLastStateProof {
    last_n_blocks: 0,
    difficulties: difficulties,
    start_number: 0,
    difficulty_boundary: total_difficulty(1_000_000),
    last_hash: <current tip hash>,
    start_hash: <genesis hash>,
}

# Server performs:
#   for each of 1000 difficulties → binary_search([i-1, 1_000_000))
#   each binary search: ~20 iterations × 2 DB reads = 40 reads
#   total: 1000 × 40 = 40,000 DB reads per single request
# Send in a tight loop → sustained IO exhaustion, no ban triggered
```