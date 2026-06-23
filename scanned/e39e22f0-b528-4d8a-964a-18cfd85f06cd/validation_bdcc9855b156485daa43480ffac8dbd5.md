### Title
Unauthenticated `clear_tx_pool` RPC Allows Any Local Caller to Destroy All Pending Transactions Without Authorization - (File: rpc/src/module/pool.rs)

### Summary
The `clear_tx_pool` and `remove_transaction` RPC methods in `rpc/src/module/pool.rs` perform privileged, destructive operations on the transaction pool with no authentication or caller-identity check. Any process that can reach the RPC port — including any local user on the same host — can invoke these methods and destroy pending transactions without being the node operator. This is the direct CKB analog of the Identity.sol "any module can change the owner" pattern: a privileged state-mutation action is callable by any party, not just the authorized owner.

### Finding Description

`clear_tx_pool` and `remove_transaction` are implemented in `PoolRpcImpl` with no caller-identity verification:

```rust
// rpc/src/module/pool.rs
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}

fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

Neither method checks who the caller is. The RPC server itself has no built-in authentication layer. The `ServiceBuilder` wires these handlers directly to the JSON-RPC dispatcher with no middleware guard:

```rust
// rpc/src/tests/setup.rs (mirrors production wiring)
let builder = ServiceBuilder::new(&rpc_config)
    .enable_pool(shared.clone(), vec![], vec![])
    ...
```

The default listen address is `127.0.0.1:8114`. Any local process — a co-located web service, a compromised dependency, a shared-host tenant, or a malicious CLI tool — can issue:

```json
{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}
```

or

```json
{"jsonrpc":"2.0","method":"remove_transaction","params":["0x<tx_hash>"],"id":1}
```

and the node will comply unconditionally.

The root cause is identical to the Identity.sol pattern: the privileged action (`clear_pool`, `remove_local_tx`) is reachable by any caller because the authorization check that should gate it is entirely absent.

### Impact Explanation

- **`clear_tx_pool`**: Atomically destroys every pending and proposed transaction in the pool. All unconfirmed transactions submitted by users are silently discarded. Miners lose all queued fee revenue. Users must rebroadcast, and time-sensitive transactions (e.g., RBF replacements, DeFi operations) may miss their window.
- **`remove_transaction`**: Allows targeted, surgical removal of a specific transaction by hash. An attacker who observes the mempool (via `get_raw_tx_pool`) can selectively censor a competitor's high-fee transaction or a specific user's transfer, preventing it from being mined without the submitter knowing until they check on-chain status.

Both operations constitute unauthorized modification of privileged node state — the direct analog of unauthorized ownership transfer.

### Likelihood Explanation

The attacker precondition is low: TCP access to `127.0.0.1:8114`. This is reachable by:
- Any process running under any user account on the same host (shared VPS, container with host-network, CI runner, etc.)
- Any application the node operator has installed that makes outbound localhost connections
- A compromised dependency in any co-located service

No credentials, keys, or elevated OS privileges are required. The RPC documentation warns operators to "strictly limit access to only trusted machines," but this is advisory only — the code enforces nothing.

### Recommendation

Add an authentication/authorization layer to the RPC server. Options in increasing strength:
1. **HTTP Basic Auth** (already partially supported per CHANGELOG entry `#2604: Allow miner http basic authorization`) — extend it to cover all privileged mutating methods.
2. **Per-method capability flags** in `RpcConfig` that gate destructive methods (`clear_tx_pool`, `remove_transaction`, `clear_banned_addresses`, `set_ban`) behind an explicit opt-in token or IP allowlist enforced in the handler, not just in documentation.
3. **Unix socket transport** for privileged methods, restricting access to OS-level file permissions.

At minimum, `clear_tx_pool` and `remove_transaction` should require a configurable secret token passed as a header or parameter, checked before the pool mutation is dispatched.

### Proof of Concept

**Precondition**: CKB node running with default config (`listen_address = "127.0.0.1:8114"`, `Pool` module enabled).

**Step 1** — Submit a transaction to the pool:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<tx_json>, "passthrough"],"id":1}'
```

**Step 2** — Confirm it is pending:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tx_pool_info","params":[],"id":2}'
# "pending": "0x1"
```

**Step 3** — As any local process (no credentials), clear the pool:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":3}'
# {"jsonrpc":"2.0","result":null,"id":3}
```

**Step 4** — Confirm pool is empty:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"tx_pool_info","params":[],"id":4}'
# "pending": "0x0"  — all transactions destroyed
```

The transaction is gone. No authentication was required at any step. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** rpc/src/module/pool.rs (L471-497)
```rust
#[derive(Clone)]
pub(crate) struct PoolRpcImpl {
    shared: Shared,
    well_known_lock_scripts: Vec<packed::Script>,
    well_known_type_scripts: Vec<packed::Script>,
}

impl PoolRpcImpl {
    pub fn new(
        shared: Shared,
        mut extra_well_known_lock_scripts: Vec<packed::Script>,
        mut extra_well_known_type_scripts: Vec<packed::Script>,
    ) -> PoolRpcImpl {
        let mut well_known_lock_scripts =
            build_well_known_lock_scripts(shared.consensus().id.as_str());
        let mut well_known_type_scripts =
            build_well_known_type_scripts(shared.consensus().id.as_str());

        well_known_lock_scripts.append(&mut extra_well_known_lock_scripts);
        well_known_type_scripts.append(&mut extra_well_known_type_scripts);

        PoolRpcImpl {
            shared,
            well_known_lock_scripts,
            well_known_type_scripts,
        }
    }
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
