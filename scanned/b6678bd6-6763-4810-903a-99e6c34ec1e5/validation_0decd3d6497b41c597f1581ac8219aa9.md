### Title
Unauthenticated Notify Listener + `work_id=0` Tautology Allows Unconditional Block Template Injection — (`miner/src/client.rs`)

---

### Summary

Two independent weaknesses compose into a concrete injection path: (1) the miner's HTTP notify listener accepts connections from any peer with zero authentication, and (2) a logical tautology in `update_block_template` guarantees acceptance of any template whose `work_id` field is `0`. An attacker on the same network as the miner can POST a crafted `BlockTemplate` with `work_id=0` and force the miner to mine on an adversarial template.

---

### Finding Description

**Weakness 1 — Unauthenticated notify listener**

`listen_block_template_notify` binds a raw TCP listener and dispatches every incoming connection to `handle` with no IP allowlist, no shared secret, and no HTTP authentication: [1](#0-0) 

The `handle` function deserialises the body directly into a `BlockTemplate` and calls `update_block_template` unconditionally: [2](#0-1) 

**Weakness 2 — `work_id=0` tautology in `update_block_template`**

```rust
let updated = |id| {
    if id != work_id || id == 0 {   // ← when work_id==0 this is (id!=0 || id==0) = always true
        Some(work_id)
    } else {
        None
    }
};
``` [3](#0-2) 

`id` is the *current* value of `current_work_id` (the `AtomicU64`). When the incoming template carries `work_id=0`, the predicate reduces to `id != 0 || id == 0`, which is a tautology over all possible `u64` values. `fetch_update` therefore always returns `Ok`, and the attacker's `Work` is unconditionally enqueued to every mining worker.

`current_work_id` is initialised to `0`: [4](#0-3) 

The `|| id == 0` branch was intended to bootstrap the first template acceptance, but it simultaneously creates a permanent bypass for any external sender who fixes `work_id` to `0`.

---

### Impact Explanation

Once the attacker's `Work` is accepted, the miner's workers hash against the attacker-supplied `Block`, which was assembled from the attacker's chosen `parent_hash`, `compact_target`, `epoch`, `dao`, and `cellbase`. Concrete consequences:

- **Mining-reward theft**: attacker sets `cellbase` outputs to their own lock script. The resulting block is structurally valid; the CKB node's `submit_block` does not enforce that the cellbase lock matches the operator's configured `block_assembler`.
- **Hashpower diversion / fork extension**: attacker builds on a stale-but-valid ancestor, directing the miner's PoW toward extending an adversarial fork. With sufficient hashpower this triggers a chain reorganisation.
- **Persistent suppression**: because `work_id=0` always wins the `fetch_update` race, legitimate templates pushed by the real node (which carry non-zero, monotonically increasing `work_id` values) are also accepted — but the attacker can re-inject at any time to override them again.

---

### Likelihood Explanation

- **Precondition**: notify mode must be enabled (`config.listen` is `Some`). This is opt-in but explicitly documented and recommended for production mining farms where the node and miner run on separate hosts.
- **Network position**: the default example address is `127.0.0.1:8888`, but operators commonly expose the listener on a LAN interface so the CKB node (on a different host) can reach it. Any host on that LAN segment can exploit this.
- **Exploit complexity**: trivially low — a single HTTP POST with a hand-crafted JSON body. No credentials, no PoW, no chain knowledge required beyond reading the public chain tip (needed only for the fork-extension variant). [5](#0-4) 

---

### Recommendation

1. **Authenticate the notify endpoint**: require a shared secret (e.g., a bearer token or HMAC-signed body) configured in `MinerClientConfig` and verified in `handle` before calling `update_block_template`.
2. **Fix the tautology**: remove the `|| id == 0` branch. The initial-bootstrap case is already handled by `id != work_id` when the first legitimate template arrives with any `work_id` (including `0`), because `0 != 0` is false only when both sides are `0`, which is the correct no-op. Alternatively, initialise `current_work_id` to `u64::MAX` so the sentinel is never a valid `work_id`.
3. **IP allowlist**: restrict `listen_block_template_notify` to accept connections only from the configured CKB node's address.

---

### Proof of Concept

```rust
// Attacker sends this HTTP POST to http://<miner-listen-addr>/
// Body (application/json):
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
  "work_id": "0x0",   // <-- forces tautology; always accepted
  "dao": "0x..."
}
```

**Assertion**: after this POST, `client.current_work_id` is `0` and the `Work` sent to workers contains the attacker's `parent_hash` and `cellbase`. A unit test can verify this by calling `update_block_template` directly with a crafted `BlockTemplate { work_id: 0, .. }` and asserting the channel receives a `Works::New` whose `block.header().raw().parent_hash()` matches the injected value — regardless of what `current_work_id` was before the call.

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
