Audit Report

## Title
Batch Transaction Relay Aborts Entirely When Any Single Transaction Has Excessive Declared Cycles — (File: sync/src/relayer/transactions_process.rs)

## Summary
In `TransactionsProcess::execute()`, if any transaction in a `RelayTransactions` batch has `declared_cycles > max_block_cycles`, the entire batch is silently dropped via an early `return Status::ok()` at line 73. Both `mark_as_known_txs` and `submit_remote_tx` are bypassed for all co-batched valid transactions. An unprivileged peer can exploit this to permanently evict specific transaction hashes from a target node's request queue by bundling them with one crafted entry carrying an inflated `declared_cycles` field.

## Finding Description
`TransactionsProcess::execute()` filters the incoming batch to only transactions the node requested from this specific peer (lines 49–55), then performs a batch-level cycles check:

```rust
// sync/src/relayer/transactions_process.rs, lines 64–74
if txs
    .iter()
    .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
{
    self.nc.ban_peer(self.peer, DEFAULT_BAN_TIME, ...);
    return Status::ok();   // entire batch dropped
}
``` [1](#0-0) 

The `return` at line 73 bypasses `mark_as_known_txs` (line 76) and the `submit_remote_tx` loop (lines 85–92): [2](#0-1) 

Because `mark_as_known_txs` is skipped, the valid co-batched transactions remain in `unknown_tx_hashes` with `requested: true` (set when `GetRelayTransactions` was sent). On the next `pop_ask_for_txs` tick, `next_request_peer()` is called on each such entry. Since `requested == true` and the attacker was the sole announcing peer (`peers.len() == 1`), it returns `None`: [3](#0-2) 

When `next_request_peer()` returns `None`, the entry is **not** re-inserted into `unknown_tx_hashes`, permanently evicting the hash from the request queue: [4](#0-3) 

The `declared_cycles` field is a plain integer in the wire format with no cryptographic binding to the transaction body at the relay layer, so it can be freely set by any peer.

## Impact Explanation
**High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker operating multiple sybil peers can systematically suppress transaction propagation to targeted nodes. Each attack attempt costs only a P2P connection and a single crafted message. At scale, this degrades mempool liveness network-wide, delays transaction confirmation, and can be used to selectively censor specific transactions from reaching specific miners or nodes.

## Likelihood Explanation
The attack requires only an unprivileged P2P connection. The attacker connects, sends `RelayTransactionHashes` for target tx hashes (standard gossip, raises no suspicion), waits for the node to issue `GetRelayTransactions`, then responds with a batch containing the requested valid transactions plus one entry with `declared_cycles = max_block_cycles + 1`. The 3-day ban is per-IP and trivially bypassed with rotating IPs, making the attack fully repeatable with no privileged access required.

## Recommendation
Replace the batch-aborting `.any()` guard with a per-transaction filter. Ban the peer when any offending transaction is detected, but continue processing the remaining valid transactions:

```rust
let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
let has_excessive = txs.iter().any(|(_, c)| c > &max_block_cycles);
if has_excessive {
    self.nc.ban_peer(self.peer, DEFAULT_BAN_TIME,
        String::from("relay declared cycles greater than max_block_cycles"));
}
let txs: Vec<_> = txs.into_iter()
    .filter(|(_, c)| c <= &max_block_cycles)
    .collect();
if txs.is_empty() {
    return Status::ok();
}
// proceed to mark_as_known_txs and submit_remote_tx
```

## Proof of Concept
1. Attacker peer connects to a CKB node.
2. Attacker sends `RelayTransactionHashes([H_valid_1, H_valid_2, H_bad])`.
3. Node calls `add_ask_for_txs`, inserting all three hashes into `unknown_tx_hashes` with `requested: false`.
4. Node's periodic `pop_ask_for_txs` fires, calls `next_request_peer()` (sets `requested: true`), and sends `GetRelayTransactions([H_valid_1, H_valid_2, H_bad])` to the attacker.
5. Attacker responds with `RelayTransactions` containing `tx_valid_1` (correct cycles), `tx_valid_2` (correct cycles), `tx_bad` (any body, `declared_cycles = max_block_cycles + 1`).
6. The filter at lines 49–55 passes all three (all were requested from this peer).
7. `.any()` at line 66 returns `true` for `tx_bad`; peer is banned; `return Status::ok()` fires at line 73.
8. `mark_as_known_txs` and `submit_remote_tx` are never called for `tx_valid_1` or `tx_valid_2`.
9. On the next `pop_ask_for_txs` tick, `next_request_peer()` returns `None` for `H_valid_1`/`H_valid_2` (sole announcer, `peers.len() == 1`), permanently evicting those hashes from the queue.
10. The node never receives or processes `tx_valid_1` or `tx_valid_2` unless another peer independently announces the same hashes.

### Citations

**File:** sync/src/relayer/transactions_process.rs (L64-74)
```rust
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```

**File:** sync/src/relayer/transactions_process.rs (L76-93)
```rust
        shared_state.mark_as_known_txs(txs.iter().map(|(tx, _)| tx.hash()));

        let tx_pool = self.relayer.shared.shared().tx_pool_controller().clone();
        let peer = self.peer;
        self.relayer
            .shared
            .shared()
            .async_handle()
            .spawn(async move {
                for (tx, declared_cycles) in txs {
                    if let Err(e) = tx_pool
                        .submit_remote_tx(tx.clone(), declared_cycles, peer)
                        .await
                    {
                        error!("submit_tx error {}", e);
                    }
                }
            });
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

**File:** sync/src/types/mod.rs (L1466-1479)
```rust
        while let Some((tx_hash, mut priority)) = unknown_tx_hashes.pop() {
            if priority.should_request(now) {
                if let Some(peer_index) = priority.next_request_peer() {
                    result
                        .entry(peer_index)
                        .and_modify(|hashes| hashes.push(tx_hash.clone()))
                        .or_insert_with(|| vec![tx_hash.clone()]);
                    unknown_tx_hashes.push(tx_hash, priority);
                }
            } else {
                unknown_tx_hashes.push(tx_hash, priority);
                break;
            }
        }
```
