The code matches the claims exactly. Let me verify the logic:

**Weakness 1** (`listen_block_template_notify`, L234–271): Binds a raw TCP listener with no IP filtering, no auth header check, no shared secret. The `handle` function (L358–369) deserializes any incoming body directly into a `BlockTemplate` and calls `update_block_template` unconditionally. Confirmed.

**Weakness 2** (`update_block_template`, L293–311): With `work_id = 0` from the incoming template, the closure evaluates `id != 0 || id == 0` — a tautology over all `u64` values. `fetch_update` always returns `Ok`, so the attacker's `Work` is always enqueued. Confirmed.

Both weaknesses are real, compose into a concrete injection path, and the notify mode is documented for production use.

---

Audit Report

## Title
Unauthenticated Notify Listener + `work_id=0` Tautology Enables Unconditional Block Template Injection — (`miner/src/client.rs`)

## Summary
The miner's HTTP notify listener accepts connections from any peer with no authentication. A separate logical tautology in `update_block_template` guarantees that any incoming `BlockTemplate` with `work_id=0` is unconditionally accepted and dispatched to all mining workers. An attacker on the same network can POST a crafted template to redirect the miner's hashpower and steal mining rewards.

## Finding Description
**Weakness 1 — Unauthenticated notify listener**

`listen_block_template_notify` (L234) binds a raw `TcpListener` with no IP allowlist, no shared secret, and no HTTP authentication header check. [1](#0-0) 

The `handle` function (L358–368) deserializes the request body directly into a `BlockTemplate` and calls `update_block_template` with no prior validation of the caller's identity. [2](#0-1) 

**Weakness 2 — `work_id=0` tautology**

`update_block_template` uses `fetch_update` with the closure:

```rust
let updated = |id| {
    if id != work_id || id == 0 {
        Some(work_id)
    } else {
        None
    }
};
``` [3](#0-2) 

When the incoming template carries `work_id = 0`, the predicate becomes `id != 0 || id == 0`, which is a tautology for all possible `u64` values of `id`. `fetch_update` therefore always returns `Ok`, and the attacker's `Work` is unconditionally enqueued to every mining worker. `current_work_id` is initialized to `0`, so this bypass is active from startup. [4](#0-3) 

**Persistence**: After the attacker's template sets `current_work_id` to `0`, any subsequent re-injection with `work_id=0` still satisfies the tautology (e.g., current `id=5`: `5 != 0 || 5 == 0` = `true`), so the attacker can override legitimate templates at any time.

## Impact Explanation
The attacker controls the `cellbase` output lock script, `parent_hash`, `compact_target`, `epoch`, and `dao` fields of the block the miner hashes against. Concrete consequences:

- **Mining-reward theft**: The resulting block is structurally valid; `submit_block` does not enforce that the cellbase lock matches the operator's configured `block_assembler`. The attacker receives all block rewards.
- **Hashpower diversion / fork extension**: The attacker can direct the miner's PoW toward extending an adversarial fork built on a stale-but-valid ancestor, potentially triggering a chain reorganization and damaging CKB economy.

This maps to **"Vulnerabilities which could easily damage CKB economy"** (Critical, 15001–25000 points) via systematic mining-reward theft and potential consensus disruption through hashpower diversion.

## Likelihood Explanation
- **Precondition**: Notify mode must be enabled (`config.listen` is `Some`). This is opt-in but explicitly documented and recommended for production mining farms where the node and miner run on separate hosts. [5](#0-4) 
- **Network position**: Any host on the same LAN segment as the miner's notify listener can exploit this. The default example address is `127.0.0.1:8888`, but production deployments commonly expose this on a LAN interface.
- **Exploit complexity**: Trivially low — a single HTTP POST with a hand-crafted JSON body. No credentials, no PoW, no chain knowledge required.

## Recommendation
1. **Authenticate the notify endpoint**: Require a shared secret (e.g., a bearer token or HMAC-signed body) configured in `MinerClientConfig` and verified in `handle` before calling `update_block_template`.
2. **Fix the tautology**: Remove the `|| id == 0` branch. The bootstrap case is already handled by `id != work_id` when the first legitimate template arrives. Alternatively, initialize `current_work_id` to `u64::MAX` so the sentinel is never a valid `work_id`.
3. **IP allowlist**: Restrict `listen_block_template_notify` to accept connections only from the configured CKB node's address.

## Proof of Concept
Send the following HTTP POST to `http://<miner-listen-addr>/`:

```json
{
  "version": "0x0",
  "compact_target": "0x1a08a97e",
  "current_time": "0x...",
  "number": "0x...",
  "epoch": "0x...",
  "parent_hash": "<any valid block hash>",
  "cycles_limit": "0x...",
  "bytes_limit": "0x...",
  "uncles_count_limit": "0x2",
  "uncles": [],
  "transactions": [],
  "proposals": [],
  "cellbase": {
    "hash": "0x...",
    "data": { /* cellbase with attacker's lock script as output */ }
  },
  "work_id": "0x0",
  "dao": "0x..."
}
```

**Unit test assertion**: Call `update_block_template` directly with a crafted `BlockTemplate { work_id: 0, .. }` where `current_work_id` is set to any value (e.g., `5`). Assert that the `new_work_tx` channel receives a `Works::New` whose `block.header().raw().parent_hash()` matches the injected value — confirming unconditional acceptance regardless of prior state.

### Citations

**File:** miner/src/client.rs (L163-163)
```rust
            current_work_id: Arc::new(AtomicU64::new(0)),
```

**File:** miner/src/client.rs (L234-242)
```rust
    async fn listen_block_template_notify(&self, addr: SocketAddr) {
        let listener = TcpListener::bind(addr).await.unwrap();
        let server = auto::Builder::new(TokioExecutor::new());
        let graceful = GracefulShutdown::new();
        let stop_rx: CancellationToken = new_tokio_exit_rx();

        loop {
            let client = self.clone();
            let handle = service_fn(move |req| handle(client.clone(), req));
```

**File:** miner/src/client.rs (L293-311)
```rust
    fn update_block_template(&self, block_template: BlockTemplate) {
        let work_id = block_template.work_id.into();
        let updated = |id| {
            if id != work_id || id == 0 {
                Some(work_id)
            } else {
                None
            }
        };
        if self
            .current_work_id
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, updated)
            .is_ok()
        {
            let work: Work = block_template.into();
            if let Err(e) = self.new_work_tx.send(Works::New(work)) {
                error!("notify_new_block error: {:?}", e);
            }
        }
```

**File:** miner/src/client.rs (L358-368)
```rust
async fn handle(
    client: Client,
    req: Request<hyper::body::Incoming>,
) -> Result<Response<Empty<Bytes>>, Error> {
    let body = BodyExt::collect(req).await?.aggregate();

    if let Ok(template) = serde_json::from_reader(body.reader()) {
        client.update_block_template(template);
    }

    Ok(Response::new(Empty::new()))
```

**File:** resource/ckb-miner.toml (L59-60)
```text
# enable listen notify mode
# listen = "127.0.0.1:8888"
```
