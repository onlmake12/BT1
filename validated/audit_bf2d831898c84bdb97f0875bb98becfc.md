Audit Report

## Title
Unvalidated Ordering of Peer-Supplied `block_locator_hashes` Breaks `unknown_header_list` Invariant in `insert_peer_unknown_header_list`, Degrading IBD Block Fetching — (File: `sync/src/types/mod.rs`)

## Summary
`GetHeadersProcess::execute` passes raw peer-controlled `block_locator_hashes` to `insert_peer_unknown_header_list`, which relies on the list being sorted highest-to-lowest to correctly populate `unknown_header_list` and set `best_known_header`. A malicious outbound peer can reverse the ordering so that `Vec::pop()` in `BlockFetcher::fetch` always yields the highest (last-to-be-downloaded) hash, causing an immediate `break` on every invocation and preventing `best_known_header` from being advanced via the `unknown_header_list` fast-path for the duration of IBD.

## Finding Description
**Population side — `insert_peer_unknown_header_list`** (`sync/src/types/mod.rs`, lines 1181–1197):

The function accepts `header_list: Vec<Byte32>` verbatim from the P2P message. Line 1184 documents the invariant ("header list is an ordered list, sorted from highest to lowest, so here you discard and exit early"), but nothing enforces it. Hashes are appended via `insert_unknown_header_hash` → `Vec::push`, preserving peer-supplied order. [1](#0-0) 

If the attacker sends all-unknown hashes in lowest-to-highest order, the loop never finds a known hash, `best_known_header` is never set by this function, and `unknown_header_list` ends up as `[H_low, ..., H_high]` (highest at the tail).

**Consumption side — `BlockFetcher::fetch`** (`sync/src/synchronizer/block_fetcher.rs`, lines 141–156):

The comment on line 143 restates the same invariant. `take_unknown_last` calls `Vec::pop()`, yielding the tail element. With the reversed list, `pop()` always returns `H_high`, which is still unknown during IBD. Every invocation hits the `else` branch on the first iteration and breaks; `best_known_header` is never advanced via this path. [2](#0-1) 

**Guard check** (`sync/src/synchronizer/get_headers_process.rs`, lines 60–64):

The only guard is peer-type membership (outbound/whitelist/protect). Any normal outbound peer satisfies this. No ordering or content validation is applied to `block_locator_hashes`. [3](#0-2) 

**`best_known_header` gate** (`sync/src/synchronizer/block_fetcher.rs`, lines 159–167):

With `best_known_header` stuck at `None` via this path, the gate causes `fetch` to return `None` for the attacker peer on every call, suppressing `GetBlocks` issuance to that peer through the `unknown_header_list` fast-path. [4](#0-3) 

**One-shot population guard** (`sync/src/types/mod.rs`, line 1183):

`insert_peer_unknown_header_list` is called only once per peer (guarded by `unknown_header_list_is_empty`). A single malformed `GetHeaders` message permanently poisons the `unknown_header_list` for that peer connection. [5](#0-4) 

## Impact Explanation
The concrete impact is targeted liveness degradation of IBD for victim nodes via the `unknown_header_list` fast-path. An attacker occupying *k* outbound slots disables the fast-path block-download optimization for those slots proportionally. `best_known_header` is not set through this path, so `BlockFetcher::fetch` returns `None` for affected peers on every call, reducing effective IBD throughput. This maps to **Low (501–2000 points): any other important performance improvements for CKB**, as it materially degrades IBD throughput and node sync liveness without crashing the node or affecting consensus. [6](#0-5) 

## Likelihood Explanation
The attacker needs only to run a reachable CKB node and wait for victim nodes to open outbound connections — a zero-cost, realistic condition. The malformed `GetHeaders` message is a single P2P packet requiring no key material, hash power, or privileged access. The `is_outbound || is_whitelist || is_protect` guard is satisfied by any normal outbound peer. The attack is repeatable across multiple outbound slots and across multiple victim nodes simultaneously. [3](#0-2) 

## Recommendation
In `insert_peer_unknown_header_list`, after collecting unknown hashes, sort `unknown_header_list` by block number in ascending order (lowest at the tail so `pop()` yields the lowest first) rather than trusting the peer-supplied ordering. Concretely: look up each hash's block number from the header map during the population loop and insert into a `BTreeMap` keyed by block number, then collect into a `Vec` sorted ascending. Alternatively, after the loop, call `unknown_header_list.sort_by_key(|h| block_number_of(h))` before returning. This ensures the consumption order in `BlockFetcher::fetch` is always correct regardless of peer-supplied ordering. [7](#0-6) 

## Proof of Concept
1. Attacker runs a CKB node reachable on the network. Victim node opens an outbound connection during IBD.
2. Attacker sends `SyncMessage::GetHeaders` with `block_locator_hashes` = `[H_low, H_low+1, ..., H_high]` (all hashes unknown to the victim, ordered lowest-to-highest).
3. `GetHeadersProcess::execute` passes the raw slice to `insert_peer_unknown_header_list`.
4. The loop finds no known hash; all hashes are pushed in order → `unknown_header_list` = `[H_low, ..., H_high]`.
5. Every subsequent call to `BlockFetcher::fetch` calls `take_unknown_last` → pops `H_high` → not in header map → pushes back → `break`. `best_known_header` remains `None` via this path.
6. `BlockFetcher::fetch` returns `None` at the `peer_best_known_header()` check; no `GetBlocks` is issued to this peer via the fast-path.
7. Repeating across multiple outbound slots degrades IBD throughput proportionally to the fraction of slots occupied.

A unit test can confirm this by constructing a `SyncShared` with a mock header map, calling `insert_peer_unknown_header_list` with a reversed locator, then asserting that repeated calls to `BlockFetcher::fetch` return `None` and that `peer_best_known_header()` remains `None` even after lower-numbered blocks are added to the header map. [1](#0-0)

### Citations

**File:** sync/src/types/mod.rs (L1181-1197)
```rust
    pub fn insert_peer_unknown_header_list(&self, pi: PeerIndex, header_list: Vec<Byte32>) {
        // update peer's unknown_header_list only once
        if self.state().peers.unknown_header_list_is_empty(pi) {
            // header list is an ordered list, sorted from highest to lowest,
            // so here you discard and exit early
            for hash in header_list {
                if let Some(header) = self.shared().header_map().get(&hash) {
                    self.state()
                        .peers
                        .may_set_best_known_header(pi, header.as_header_index());
                    break;
                } else {
                    self.state().peers.insert_unknown_header_hash(pi, hash)
                }
            }
        }
    }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L141-156)
```rust
        if let IBDState::In = self.ibd {
            let state = self.sync_shared.state();
            // unknown list is an ordered list, sorted from highest to lowest,
            // when header hash unknown, break loop is ok
            while let Some(hash) = state.peers().take_unknown_last(self.peer) {
                // Here we need to first try search from headermap, if not, fallback to search from the db.
                // if not search from db, it can stuck here when the headermap may have been removed just as the block was downloaded
                if let Some(header) = self.sync_shared.get_header_index_view(&hash, false) {
                    state
                        .peers()
                        .may_set_best_known_header(self.peer, header.as_header_index());
                } else {
                    state.peers().insert_unknown_header_hash(self.peer, hash);
                    break;
                }
            }
```

**File:** sync/src/synchronizer/block_fetcher.rs (L159-168)
```rust
        let best_known = match self.peer_best_known_header() {
            Some(t) => t,
            None => {
                debug!(
                    "Peer {} doesn't have best known header; ignore it",
                    self.peer
                );
                return None;
            }
        };
```

**File:** sync/src/synchronizer/get_headers_process.rs (L60-64)
```rust
            if let Some(flag) = shared.state().peers().get_flag(self.peer)
                && (flag.is_outbound || flag.is_whitelist || flag.is_protect)
            {
                shared.insert_peer_unknown_header_list(self.peer, block_locator_hashes);
            };
```
