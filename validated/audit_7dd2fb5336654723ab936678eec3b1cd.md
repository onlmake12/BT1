### Title
Unauthenticated `remove_transaction` RPC Allows Any Local Caller to Silently Evict Any Pending Transaction — (`rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` JSON-RPC method in CKB's Pool module performs no ownership check or caller authentication. Any caller with access to the RPC port can remove any pending transaction — and all of its descendants — from the tx-pool at zero cost. Because the Pool module is enabled by default and the RPC server is reachable by any local process, this is a direct, sustained, zero-cost DOS against specific victim transactions, directly analogous to the Particle `addCredit()` griefing pattern.

---

### Finding Description

**Root cause — no ownership check in `remove_transaction`:**

`rpc/src/module/pool.rs` line 254–255 declares the method with no authentication guard:

```rust
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

The implementation at lines 662–669 passes the hash directly to the pool controller with no check that the caller owns or submitted the transaction:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) [2](#0-1) 

**Pool module is enabled by default:**

The production config enables the Pool module unconditionally:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [3](#0-2) 

There is no per-method access control inside the module — all Pool methods are registered together: [4](#0-3) 

**The underlying pool operation removes the target AND all descendants:**

`tx-pool/src/process.rs` `remove_tx` removes the transaction from the verify queue, orphan pool, and main pool: [5](#0-4) 

`tx-pool/src/pool.rs` `remove_tx` calls `remove_entry_and_descendants`, wiping the entire descendant chain in one call: [6](#0-5) 

**Attacker entry path:**

The RPC server binds to `127.0.0.1:8114` by default. The scope explicitly includes "RPC caller" and "supported local CLI/RPC user" as valid unprivileged attacker profiles. Any local process — a co-tenant in a shared hosting environment, a compromised service running on the same host, or a malicious script executed by the node operator — can reach this endpoint without any credential.

The attacker workflow:
1. Observe the tx-pool via `get_raw_tx_pool` (also unauthenticated, same module) to enumerate pending transaction hashes.
2. Call `remove_transaction(victim_tx_hash)` — zero fee, zero cost.
3. Repeat every time the victim resubmits.

The `remove_transaction` call removes the target and every descendant in a single atomic operation, so a chain of 25 transactions (the `max_ancestors_count` limit) is wiped with one RPC call.

---

### Impact Explanation

- **Sustained, zero-cost DOS of specific transactions.** An attacker can prevent any targeted transaction from ever being committed by repeatedly removing it from the pool the moment it is resubmitted.
- **Cascading eviction.** Because `remove_entry_and_descendants` is called, a single call can evict up to `max_ancestors_count` (default 25) chained transactions simultaneously.
- **No funds are directly stolen**, but the victim's transaction is permanently blocked from confirmation for as long as the attacker continues the attack, which is economically free.
- **Severity analog:** This matches the Particle M-02 pattern exactly — unprivileged caller, minimal cost, shared state modification, sustained DOS of legitimate operations.

---

### Likelihood Explanation

- The Pool RPC module is **enabled by default** in every production node.
- The RPC listens on localhost by default, but "local RPC user" is an explicitly in-scope attacker profile.
- In practice, nodes are frequently deployed in shared cloud environments, behind reverse proxies, or with the RPC port exposed to a local network — all of which expand the attacker surface.
- The attack requires no special knowledge beyond the victim's transaction hash, which is observable from `get_raw_tx_pool` or public mempool explorers.
- The cost to the attacker is a single HTTP request per removal cycle.

---

### Recommendation

1. **Add caller-identity binding.** Record which local caller (e.g., by connection identity or a submitted-by token) submitted each transaction, and reject `remove_transaction` calls that do not match the submitter.
2. **Move `remove_transaction` to a restricted sub-module** (e.g., `Debug` or a new `Admin` module) that is disabled by default, analogous to how `clear_tx_pool` and `clear_tx_verify_queue` could be treated.
3. **Add a minimum rate limit** on `remove_transaction` per connection to raise the cost of sustained griefing.
4. **Document the risk** prominently in the RPC reference so operators who expose the Pool module understand that `remove_transaction` carries no ownership enforcement.

---

### Proof of Concept

```bash
# Step 1: Victim submits a transaction
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<victim_tx>, "passthrough"]}'
# Returns: {"result": "0xVICTIM_TX_HASH"}

# Step 2: Attacker enumerates pool (no auth required)
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# Returns all pending tx hashes including 0xVICTIM_TX_HASH

# Step 3: Attacker removes victim's transaction (no auth, no fee, no ownership check)
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"remove_transaction","params":["0xVICTIM_TX_HASH"]}'
# Returns: {"result": true}
# Victim's tx and all descendants are gone from the pool.

# Step 4: Repeat indefinitely at zero cost.
```

The victim's transaction can never be committed as long as the attacker loops step 3 faster than the victim can resubmit.

### Citations

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
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

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```

**File:** rpc/src/service_builder.rs (L65-77)
```rust
    pub fn enable_pool(
        mut self,
        shared: Shared,
        extra_well_known_lock_scripts: Vec<Script>,
        extra_well_known_type_scripts: Vec<Script>,
    ) -> Self {
        let methods = PoolRpcImpl::new(
            shared,
            extra_well_known_lock_scripts,
            extra_well_known_type_scripts,
        );
        set_rpc_module_methods!(self, "Pool", pool_enable, add_pool_rpc_methods, methods)
    }
```

**File:** tx-pool/src/process.rs (L440-455)
```rust
    pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
        let id = ProposalShortId::from_tx_hash(&tx_hash);
        {
            let mut queue = self.verify_queue.write().await;
            if queue.remove_tx(&id).is_some() {
                return true;
            }
        }
        {
            let mut orphan = self.orphan.write().await;
            if orphan.remove_orphan_tx(&id).is_some() {
                return true;
            }
        }
        let mut tx_pool = self.tx_pool.write().await;
        tx_pool.remove_tx(&id)
```

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
    }
```
