### Title
Inverted Relay-Gate in `is_allowed_relay()` Enables Repeated Processing of Permanently-Rejected Transactions via Malicious Peer — (`util/types/src/core/tx_pool.rs`)

---

### Summary

`Reject::is_allowed_relay()` is the boolean gate that controls whether a rejected transaction's hash is removed from the node's `known_txs` relay filter. Due to an incomplete/inverted condition, it returns `true` for permanently-invalid rejection reasons — including `Reject::Duplicated`, `Reject::ExceededTransactionSizeLimit`, `Reject::Expiry`, `Reject::Invalidated`, and `Reject::RBFRejected` — when it should return `false`. A malicious connected peer can exploit this to force the node into an indefinite loop of requesting, deserializing, and processing the same permanently-rejected transaction, constituting a targeted resource-exhaustion DoS.

---

### Finding Description

**Root cause — `util/types/src/core/tx_pool.rs`, lines 110–113:**

```rust
pub fn is_allowed_relay(&self) -> bool {
    matches!(self, Reject::DeclaredWrongCycles(..))
        || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
}
```

The comment states the intent: allow relay only for `DeclaredWrongCycles` (so the peer can re-relay with correct cycles) and for *temporary* rejections (pool full, double-spend race, expired clearing). The second arm is supposed to capture only those temporary cases. However, the condition `!LowFeeRate && !malformed` is far too broad — it also returns `true` for every permanently-invalid rejection that is neither `LowFeeRate` nor structurally malformed:

| Reject variant | `is_malformed_tx()` | `is_allowed_relay()` | Correct? |
|---|---|---|---|
| `Duplicated` | false | **true** | **NO** — tx already in pool |
| `ExceededTransactionSizeLimit` | false | **true** | **NO** — permanently too large |
| `Expiry` | false | **true** | **NO** — permanently expired |
| `Invalidated` | false | **true** | **NO** — inputs consumed |
| `RBFRejected` | false | **true** | **NO** — RBF permanently failed |
| `Full` | false | true | OK — temporary |
| `ExceededMaximumAncestorsCount` | false | true | OK — temporary |
| `LowFeeRate` | false | false | OK |
| `Malformed` | true | false | OK |

**Exploit path — three-file chain:**

**Step 1.** A remote peer sends a `RelayTransactionHashes` P2P message containing a tx hash `H` for a transaction already in the node's pool (or one that is permanently too large).

**Step 2.** The node does not find `H` in `known_txs`, so it sends `GetRelayTransactions` and receives the full transaction.

**Step 3.** `submit_remote_tx` → `_process_tx` → `pre_check` → `check_txid_collision` returns `Reject::Duplicated` (or `non_contextual_verify` returns `Reject::ExceededTransactionSizeLimit`).

**Step 4.** `after_process()` (`tx-pool/src/process.rs:517–521`) evaluates `reject.is_allowed_relay()` → `true` → sends `TxVerificationResult::Reject { tx_hash }` to the relayer:

```rust
if reject.is_allowed_relay() {
    self.send_result_to_relayer(TxVerificationResult::Reject {
        tx_hash: tx_hash.clone(),
    });
}
```

**Step 5.** The relayer's `send_bulk_of_tx_hashes` (`sync/src/relayer/mod.rs:673–675`) processes the `Reject` result:

```rust
TxVerificationResult::Reject { tx_hash } => {
    self.shared.state().remove_from_known_txs(&tx_hash);
}
```

`H` is removed from `known_txs`. The node now has no memory of having seen this tx hash.

**Step 6.** The peer re-sends `RelayTransactionHashes` with `H`. The node asks for the full tx again. Go to Step 3. The loop repeats indefinitely.

The same path is triggered via the reject callback registered in `shared/src/shared_builder.rs:587–593`, which fires during reorg-driven pool eviction.

---

### Impact Explanation

A single malicious peer with a standard P2P connection can force the victim node to:

- Repeatedly send `GetRelayTransactions` network messages
- Receive and deserialize a full transaction (up to 512 KB for `ExceededTransactionSizeLimit`)
- Execute `pre_check` (lock acquisition, txid collision check, outpoint resolution) on every cycle
- Burn CPU and I/O proportional to the attacker's send rate

For `Reject::ExceededTransactionSizeLimit` the tx is permanently invalid regardless of chain state, so the loop never self-terminates. For `Reject::Duplicated` the loop persists as long as the tx remains in the pool. The node's ability to process legitimate transactions is degraded. No funds are at risk, but sustained service degradation is achievable by an unprivileged peer.

---

### Likelihood Explanation

Any peer that can establish a standard P2P relay connection (no keys, no stake, no special role) can trigger this. The attacker needs only to know one tx hash that is already in the victim's mempool (trivially observable via `get_raw_tx_pool` RPC or by submitting the tx themselves) or to craft a tx slightly over 512 KB. The attack is low-cost for the attacker and high-cost for the victim. Likelihood is **medium-high** given the ease of setup and the absence of any per-peer rate limit on `RelayTransactionHashes` messages that would break the loop.

---

### Recommendation

`is_allowed_relay()` must explicitly exclude all permanently-invalid rejection reasons. The minimal fix is to add `Reject::Duplicated`, `Reject::ExceededTransactionSizeLimit`, `Reject::Expiry`, `Reject::Invalidated`, and `Reject::RBFRejected` to the exclusion list, mirroring the intent of the existing `LowFeeRate` exclusion:

```rust
pub fn is_allowed_relay(&self) -> bool {
    matches!(self, Reject::DeclaredWrongCycles(..))
        || (!matches!(
                self,
                Reject::LowFeeRate(..)
                    | Reject::Duplicated(..)
                    | Reject::ExceededTransactionSizeLimit(..)
                    | Reject::Expiry(..)
                    | Reject::Invalidated(..)
                    | Reject::RBFRejected(..)
            )
            && !self.is_malformed_tx())
}
```

Alternatively, invert the logic to an explicit allowlist of temporary rejections (`Full`, `ExceededMaximumAncestorsCount`, `Resolve` for non-permanent errors) rather than a blocklist, which is safer against future variant additions.

---

### Proof of Concept

**Preconditions:** Attacker has a standard P2P relay connection to the victim node.

**Steps:**

1. Attacker submits transaction `T` (valid, fee-paying) to the victim node via RPC or relay. `T` is now in the victim's mempool with hash `H`.

2. Attacker sends a `RelayTransactionHashes` P2P message containing `H`.

3. Victim node checks `known_txs` — `H` is absent (it was never added via the relay path) — and responds with `GetRelayTransactions`.

4. Attacker sends `RelayTransactions` containing `T` with any declared cycle count.

5. Victim processes `T`: `pre_check` → `check_txid_collision` → `Reject::Duplicated`.

6. `after_process` calls `is_allowed_relay()` → `true` → `TxVerificationResult::Reject{H}` → `remove_from_known_txs(H)`.

7. Attacker immediately re-sends `RelayTransactionHashes` with `H`. Victim asks for `T` again. Repeat from step 4.

**Observable effect:** Victim node's CPU and network I/O increase proportionally to the attacker's send rate. Legitimate tx processing latency increases. The loop continues until the attacker disconnects or `T` is evicted from the pool.

**Variant (no mempool dependency):** Craft a transaction `T'` with serialized size = 513 KB (just over `TRANSACTION_SIZE_LIMIT = 512 * 1_000`). Submit via relay. Victim rejects with `Reject::ExceededTransactionSizeLimit`, `is_allowed_relay()` → `true`, `remove_from_known_txs`. Attacker re-sends hash. Loop is permanent since `T'` is never in the pool and always too large. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** util/types/src/core/tx_pool.rs (L104-113)
```rust
    /// Returns true if tx can be resubmitted, allowing relay
    /// * Declared wrong cycles should allow relay with the correct cycles
    /// * Reject but is not malformed and the fee rate reached the threshold,
    ///   it may be due to double spending
    ///   or temporary limitations of the pool resources,
    ///   and expired clearing
    pub fn is_allowed_relay(&self) -> bool {
        matches!(self, Reject::DeclaredWrongCycles(..))
            || (!matches!(self, Reject::LowFeeRate(..)) && !self.is_malformed_tx())
    }
```

**File:** tx-pool/src/process.rs (L513-525)
```rust
                    } else {
                        if reject.is_malformed_tx() {
                            self.ban_malformed(peer, format!("reject {reject}")).await;
                        }
                        if reject.is_allowed_relay() {
                            self.send_result_to_relayer(TxVerificationResult::Reject {
                                tx_hash: tx_hash.clone(),
                            });
                        }
                        if reject.should_recorded() {
                            self.put_recent_reject(&tx_hash, reject).await;
                        }
                    }
```

**File:** shared/src/shared_builder.rs (L587-593)
```rust
            if reject.is_allowed_relay()
                && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject {
                    tx_hash: tx_hash.clone(),
                })
            {
                error!("tx-pool tx_relay_sender internal error {}", e);
            }
```

**File:** sync/src/relayer/mod.rs (L673-675)
```rust
                    TxVerificationResult::Reject { tx_hash } => {
                        self.shared.state().remove_from_known_txs(&tx_hash);
                    }
```
