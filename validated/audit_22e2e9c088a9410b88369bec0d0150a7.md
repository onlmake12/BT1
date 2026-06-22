### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any Pending Transaction from the Pool - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC endpoint in CKB's Pool module performs no authentication or ownership check before removing a transaction from the mempool. Any RPC caller — including any local process or any remote client if the RPC is network-exposed — can evict any other user's pending transaction by its hash. An attacker who monitors the mempool can continuously front-run a victim's resubmissions, permanently preventing a specific transaction from ever being confirmed.

---

### Finding Description

The `remove_transaction` handler in `rpc/src/module/pool.rs` is:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

There is no check that the caller owns the transaction, no API key, no IP allowlist enforced at the handler level, and no rate limit. The `Pool` module is in the default-enabled RPC module set. [2](#0-1) 

The RPC server binds to `127.0.0.1:8114` by default, but CKB explicitly supports configuring `rpc.listen_address` to any interface, making the endpoint reachable from the network in operator-supported deployments. Even when local-only, any process running on the same host — including malicious software — qualifies as a "supported local CLI/RPC user," which the scope explicitly lists as a valid attacker profile.

The `tx_hash` argument is the only input. The pool controller's `remove_local_tx` path removes the entry unconditionally: [3](#0-2) 

The removed transaction is not re-queued or re-broadcast; the submitter must resubmit it manually.

---

### Impact Explanation

**Persistent targeted transaction censorship.** An attacker who can reach the RPC endpoint:

1. Subscribes to the `new_transaction` notification topic (also unauthenticated, in the default-enabled `Subscription` module) or polls `get_raw_tx_pool`.
2. Observes a victim's transaction hash the moment it enters the pool.
3. Immediately calls `remove_transaction` with that hash.
4. Repeats on every resubmission.

The victim's transaction is perpetually evicted and can never advance to the `proposed` or `committed` state. Because the `remove_transaction` call is synchronous and cheap (a single channel message), the attacker can sustain this indefinitely at negligible cost. This is a permanent, targeted denial of transaction confirmation — directly analogous to the `vestFor` pattern of locking a user out of a protocol action with a zero-cost, unauthenticated call.

---

### Likelihood Explanation

- **Network-exposed RPC (operator-supported config):** Any remote IP can call `remove_transaction`. No credentials required. The attack is trivially scriptable.
- **Localhost-only RPC (default config):** Any co-resident process — including malware, a compromised dependency, or a second user on a shared host — can call it. The "supported local CLI/RPC user" attacker profile is explicitly in scope.
- The `Pool` module is enabled by default; no special configuration is needed to expose the endpoint.
- Transaction hashes are public (visible in `get_raw_tx_pool`, relayed over P2P), so the attacker does not need any privileged information.

---

### Recommendation

Add an ownership or caller-identity check to `remove_transaction`. Concretely:

1. **Restrict to a configurable allowlist of caller IPs** enforced at the RPC server layer (analogous to how Bitcoin Core gates `stop`, `setban`, etc. behind an RPC auth token).
2. **Or** move `remove_transaction` to a separate, non-default RPC module (e.g., `Debug` or `Admin`) that operators must explicitly enable and should never expose publicly.
3. **Or** require the caller to supply a proof of ownership (e.g., a signature over the tx hash with the key that signed the transaction's inputs), so only the original submitter can remove it.

The same audit should be applied to `clear_tx_pool` and `clear_tx_verify_queue`, which are equally unauthenticated and affect all users simultaneously. [4](#0-3) 

---

### Proof of Concept

**Preconditions:** Attacker has TCP access to the CKB node's RPC port (either via network-exposed config or localhost).

**Steps:**

1. Victim submits a transaction `T` via `send_transaction`. Its hash `H` is now visible in `get_raw_tx_pool`.
2. Attacker polls `get_raw_tx_pool` (or subscribes to `new_transaction` notifications).
3. Attacker sends:
   ```json
   {"id":1,"jsonrpc":"2.0","method":"remove_transaction","params":["<H>"]}
   ```
4. Node responds `true`; `T` is gone from the pool.
5. Victim resubmits `T`; attacker repeats step 3 within milliseconds.
6. `T` never reaches `proposed` or `committed` status. The victim's cells remain unspent and inaccessible for the duration of the attack.

**Expected result:** `get_transaction` for `H` returns `status: "unknown"` or `status: "rejected"` indefinitely, with no on-chain confirmation.

### Citations

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

**File:** rpc/src/module/pool.rs (L684-701)
```rust
    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }

    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** rpc/src/module/mod.rs (L9-11)
```rust
//! methods by modules. The default enabled ones are enabled modules are "Net", "Pool", "Miner",
//! "Chain", "Stats", "Subscription", "Experiment". As you can see, the `Rpc` suffix is removed in
//! the config file.
```

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```
