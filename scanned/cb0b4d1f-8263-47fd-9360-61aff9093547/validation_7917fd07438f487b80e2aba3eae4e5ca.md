Audit Report

## Title
Unvalidated Ordering of Peer-Supplied `block_locator_hashes` Breaks Early-Exit Invariant in `insert_peer_unknown_header_list`, Disrupting IBD Block Fetching — (`sync/src/types/mod.rs`)

## Summary
During IBD, `GetHeadersProcess::execute` passes raw, peer-controlled `block_locator_hashes` directly to `insert_peer_unknown_header_list`, which relies on the list being sorted highest-to-lowest to correctly populate `unknown_header_list` and set `best_known_header`. A malicious outbound peer can reverse the ordering so that `Vec::pop()` in the consumption loop always yields the highest (last-to-be-downloaded) hash, causing an immediate `break` every time `BlockFetcher::fetch` runs and leaving `best_known_header` permanently `None` for that peer. With `best_known_header` unset, `BlockFetcher::fetch` returns `None` at the `peer_best_known_header()` guard and never issues `GetBlocks` to that peer for the duration of IBD.

## Finding Description
**Population side — `insert_peer_unknown_header_list`** (`sync/src/types/mod.rs`, lines 1181–1197):

The function accepts `header_list: Vec<Byte32>` verbatim from the P2P message. The comment on line 1184 documents the invariant ("header list is an ordered list, sorted from highest to lowest, so here you discard and exit early"), but nothing enforces it. Hashes are appended via `insert_unknown_header_hash` → `Vec::push`, preserving peer-supplied order.

If the attacker sends all-unknown hashes in lowest-to-highest order, the loop never finds a known hash, so `best_known_header` is never set by this function, and `unknown_header_list` ends up as `[low, ..., high]` (highest at the tail).

**Consumption side — `BlockFetcher::fetch`** (`sync/src/synchronizer/block_fetcher.rs`, lines 145–156):

```rust
while let Some(hash) = state.peers().take_unknown_last(self.peer) {
    if let Some(header) = self.sync_shared.get_header_index_view(&hash, false) {
        state.peers().may_set_best_known_header(self.peer, header.as_header_index());
    } else {
        state.peers().insert_unknown_header_hash(self.peer, hash);
        break;  // fires immediately when highest hash is still unknown
    }
}
```

`take_unknown_last` calls `Vec::pop()`, yielding the tail element. With the reversed list, `pop()` always returns the highest hash, which is the last to be downloaded during IBD. Every invocation of `fetch` hits the `else` branch on the first iteration and breaks. `best_known_header` is never advanced.

**Guard check** (`sync/src/synchronizer/get_headers_process.rs`, lines 60–64):

```rust
if let Some(flag) = shared.state().peers().get_flag(self.peer)
    && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
{
    shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
}
```

The only guard is peer-type membership (outbound/whitelist/protect). Any normal outbound peer satisfies this. No ordering or content validation is applied to `block_locator_hashes`.

**`best_known_header` gate** (`sync/src/synchronizer/block_fetcher.rs`, lines 159–167):

```rust
let best_known = match self.peer_best_known_header() {
    Some(t) => t,
    None => { return None; }  // no GetBlocks ever sent to this peer
};
```

With `best_known_header` stuck at `None`, this gate causes `fetch` to return `None` for the attacker peer on every call.

## Impact Explanation
The concrete impact is a targeted liveness degradation of IBD for victim nodes. An attacker occupying *k* of the victim's outbound slots reduces effective block-download bandwidth by a factor proportional to *k*. If all outbound slots are occupied, the victim node cannot complete IBD via the `unknown_header_list` fast-path at all. This maps to **Low (501–2000 points): any other important performance improvements for CKB**, as it materially degrades IBD throughput and node sync liveness without crashing the node or affecting consensus.

## Likelihood Explanation
The attacker needs only to run a reachable CKB node and wait for victim nodes to open outbound connections — a realistic, zero-cost condition. The malformed `GetHeaders` message is a single P2P packet requiring no key material, hash power, or privileged access. The `is_outbound || is_whitelist || is_protect` guard is satisfied by any normal outbound peer. The attack is repeatable across multiple outbound slots and across multiple victim nodes simultaneously.

## Recommendation
In `insert_peer_unknown_header_list`, after collecting unknown hashes, sort `unknown_header_list` by block number in ascending order (lowest at the tail so `pop()` yields the lowest first) rather than trusting the peer-supplied ordering. Concretely: look up each hash's block number from the header map or store during the population loop and insert into a structure (e.g., a `BTreeMap` keyed by block number, then collect into a `Vec` sorted ascending) so that the consumption order is always correct regardless of peer-supplied ordering. Alternatively, after the loop, call `unknown_header_list.sort_by_key(|h| block_number_of(h))` before returning.

## Proof of Concept
1. Attacker runs a CKB node reachable on the network. Victim node opens an outbound connection during IBD.
2. Attacker sends `SyncMessage::GetHeaders` with `block_locator_hashes` = `[H_low, H_low+1, ..., H_high]` (all hashes unknown to the victim, ordered lowest-to-highest).
3. `GetHeadersProcess::execute` passes the raw slice to `insert_peer_unknown_header_list`.
4. The loop finds no known hash; all hashes are pushed in order → `unknown_header_list` = `[H_low, ..., H_high]`.
5. Every subsequent call to `BlockFetcher::fetch` calls `take_unknown_last` → pops `H_high` → not in header map → pushes back → `break`. `best_known_header` remains `None`.
6. `BlockFetcher::fetch` returns `None` at the `peer_best_known_header()` check; no `GetBlocks` is ever sent to this peer.
7. Repeating across multiple outbound slots degrades IBD throughput proportionally to the fraction of slots occupied.

A unit test can confirm this by constructing a `SyncShared` with a mock header map, calling `insert_peer_unknown_header_list` with a reversed locator, then asserting that repeated calls to `BlockFetcher::fetch` return `None` and that `peer_best_known_header()` remains `None` even after lower-numbered blocks are added to the header map.