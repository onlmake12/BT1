Audit Report

## Title
Unbounded `peers` Vec Growth in `UnknownTxHashPriority` via Repeated `RelayTransactionHashes` — (`sync/src/types/mod.rs`, `sync/src/relayer/transaction_hashes_process.rs`)

## Summary
`push_peer()` appends a `PeerIndex` to `UnknownTxHashPriority::peers` unconditionally with no deduplication or cap. The overflow guard in `add_ask_for_txs` only checks the number of unique hash keys in `unknown_tx_hashes`, not the size of individual `peers` Vecs. A single remote peer can repeatedly announce the same phantom transaction hashes, causing each entry's `peers` Vec to grow without bound, ultimately exhausting the node's memory.

## Finding Description

**Entry point** — `TransactionHashesProcess::execute()` filters hashes through `tx_filter`, which only contains hashes of transactions that were actually received and verified. Phantom hashes (for non-existent transactions) are never added to `tx_filter` via `mark_as_known_txs`, so they pass the filter on every repeated announcement. [1](#0-0) 

**Unconditional append** — For a hash already present in `unknown_tx_hashes`, `add_ask_for_txs` clones the existing `UnknownTxHashPriority`, calls `push_peer(peer_index)`, and writes it back. `push_peer` is a bare `Vec::push` with no deduplication or length check. [2](#0-1) [3](#0-2) 

**Guard is bypassed** — The overflow check fires only when `unknown_tx_hashes.len()` (the count of unique hash keys) reaches `MAX_UNKNOWN_TX_HASHES_SIZE` (50 000). If the attacker sends N hashes repeatedly, the key count stays at N. The guard never triggers, and the `peers` Vec for each entry grows by 1 on every repetition. [4](#0-3) 

**Drain rate is negligible** — `pop_ask_for_txs` (fired every 100 ms via `ASK_FOR_TXS_TOKEN`) calls `next_request_peer()`, which removes at most one `PeerIndex` per hash per `RETRY_ASK_TX_TIMEOUT_INCREASE` (30 s). With 100 hashes, the drain is ~3.3 entries/second total. [5](#0-4) [6](#0-5) [7](#0-6) 

**Injection rate** — The rate limiter allows 30 messages/second per `(PeerIndex, message_type)`. Each message may carry up to `MAX_RELAY_TXS_NUM_PER_BATCH` = 32 767 hashes. With 100 phantom hashes, the attacker injects 30 × 100 = 3 000 `PeerIndex` entries/second, net growth ~2 997 entries/second. [8](#0-7) [9](#0-8) 

## Impact Explanation

Each `PeerIndex` is a `usize` (8 bytes on 64-bit). At ~3 000 entries/second across 100 hashes, memory grows at ~24 KB/s. After minutes to hours (depending on available RAM), the process is OOM-killed by the OS. This constitutes a complete denial of service of the victim CKB node: no block production, no transaction relay, no RPC responses.

**Allowed impact matched: High — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

- Requires only a single connected relay peer — no special privileges, no PoW, no keys.
- The attacker announces phantom tx hashes (hashes for transactions that do not exist on the network). The victim never fetches the actual transactions, so the hashes are never added to `tx_filter` and the attack sustains indefinitely.
- The rate limiter (30 req/s) slows but does not stop the attack; a single peer can exhaust gigabytes of RAM within minutes to hours depending on node RAM.
- The attack is fully repeatable and requires no coordination beyond a single TCP connection.

## Recommendation

1. **Deduplicate in `push_peer`**: Before appending, check `self.peers.contains(&peer_index)` and skip if already present.
2. **Cap `peers` Vec length**: Enforce a hard maximum equal to the number of connected peers or a small constant (e.g., 8).
3. **Use `HashSet<PeerIndex>`** instead of `Vec<PeerIndex>` in `UnknownTxHashPriority` to get O(1) deduplication automatically.
4. **Per-peer hash announcement tracking**: Maintain a per-peer LRU set of recently announced hashes and drop duplicates before calling `add_ask_for_txs`.

## Proof of Concept

```
1. Connect to a CKB node as a relay peer.
2. Generate 100 random 32-byte values as fake tx hashes (hashes for non-existent transactions).
3. In a loop at ≤30 iterations/second (within the rate limit):
   a. Send a RelayTransactionHashes message containing all 100 hashes.
4. After 1000 iterations:
   - Each entry in unknown_tx_hashes will have peers.len() == 1000.
   - Total PeerIndex entries: 100 × 1000 = 100 000 (800 KB).
5. Continue until RSS of the ckb process grows to available RAM and the process is OOM-killed.
```

The `peers.len()` grows by exactly 1 per repetition per hash because `push_peer` appends unconditionally and the overflow guard never fires (queue length stays at 100 unique keys, well below `MAX_UNKNOWN_TX_HASHES_SIZE` = 50 000). [2](#0-1) [10](#0-9)

### Citations

**File:** sync/src/relayer/transaction_hashes_process.rs (L38-49)
```rust
        let tx_hashes: Vec<_> = {
            let mut tx_filter = state.tx_filter();
            tx_filter.remove_expired();
            self.message
                .tx_hashes()
                .iter()
                .map(|x| x.to_entity())
                .filter(|tx_hash| !tx_filter.contains(tx_hash))
                .collect()
        };

        state.add_ask_for_txs(self.peer, tx_hashes)
```

**File:** sync/src/types/mod.rs (L1276-1289)
```rust
    pub fn next_request_peer(&mut self) -> Option<PeerIndex> {
        if self.requested {
            if self.peers.len() > 1 {
                self.request_time = Instant::now();
                self.peers.swap_remove(0);
                self.peers.first().cloned()
            } else {
                None
            }
        } else {
            self.requested = true;
            self.peers.first().cloned()
        }
    }
```

**File:** sync/src/types/mod.rs (L1291-1293)
```rust
    pub fn push_peer(&mut self, peer_index: PeerIndex) {
        self.peers.push(peer_index);
    }
```

**File:** sync/src/types/mod.rs (L1490-1494)
```rust
            match unknown_tx_hashes.entry(tx_hash) {
                keyed_priority_queue::Entry::Occupied(entry) => {
                    let mut priority = entry.get_priority().clone();
                    priority.push_peer(peer_index);
                    entry.set_priority(priority);
```

**File:** sync/src/types/mod.rs (L1507-1529)
```rust
        if unknown_tx_hashes.len() >= MAX_UNKNOWN_TX_HASHES_SIZE
            || unknown_tx_hashes.len()
                >= self.peers.state.len() * MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER
        {
            warn!(
                "unknown_tx_hashes is too long, len: {}",
                unknown_tx_hashes.len()
            );

            let mut peer_unknown_counter = 0;
            for (_hash, priority) in unknown_tx_hashes.iter() {
                for peer in priority.peers.iter() {
                    if *peer == peer_index {
                        peer_unknown_counter += 1;
                    }
                }
            }
            if peer_unknown_counter >= MAX_UNKNOWN_TX_HASHES_SIZE_PER_PEER {
                return StatusCode::TooManyUnknownTransactions.into();
            }

            return Status::ignored();
        }
```

**File:** sync/src/relayer/mod.rs (L89-92)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);
```

**File:** sync/src/relayer/mod.rs (L801-803)
```rust
        nc.set_notify(Duration::from_millis(100), ASK_FOR_TXS_TOKEN)
            .await
            .expect("set_notify at init is ok");
```

**File:** util/constant/src/sync.rs (L57-57)
```rust
pub const RETRY_ASK_TX_TIMEOUT_INCREASE: Duration = Duration::from_secs(30);
```

**File:** util/constant/src/sync.rs (L68-68)
```rust
pub const MAX_RELAY_TXS_NUM_PER_BATCH: usize = 32767;
```

**File:** util/constant/src/sync.rs (L70-70)
```rust
pub const MAX_UNKNOWN_TX_HASHES_SIZE: usize = 50000;
```
