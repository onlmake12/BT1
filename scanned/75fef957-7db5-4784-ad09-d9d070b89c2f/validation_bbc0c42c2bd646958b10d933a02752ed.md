### Title
Unbounded Secp256k1 Signature Recovery in `send_alert` RPC with No Rate Limiting - (File: `rpc/src/module/alert.rs`)

### Summary
The `send_alert` JSON-RPC endpoint performs multi-signature secp256k1 ECDSA recovery on every call with no rate limiting, no per-caller throttling, and no connection-level guard. Any caller with access to the RPC port can flood the endpoint with crafted alert payloads, driving sustained CPU load on the node.

### Finding Description

`send_alert` is the only RPC method in the `Alert` module. Its implementation in `AlertRpcImpl::send_alert` performs two operations before returning an error or success:

1. A timestamp check (`notice_until < now_ms`) — trivially bypassed by setting any future timestamp.
2. A call to `self.verifier.verify_signatures(&alert)`. [1](#0-0) 

`verify_signatures` in `util/network-alert/src/verifier.rs` computes `calc_alert_hash()` (a Blake2b hash), parses each submitted signature via `Signature::from_slice`, and then passes all valid-looking signatures to `verify_m_of_n`. [2](#0-1) 

`verify_m_of_n` in `util/multisig/src/secp256k1.rs` calls `sig.recover(message)` — a full secp256k1 ECDSA public-key recovery — for every signature in the submitted array, up to `pks.len()` (4 on mainnet before `SigCountOverflow` is returned). [3](#0-2) 

The RPC server (`rpc/src/server.rs`) applies no per-IP rate limiting, no per-method throttling, and no connection-count guard. The only optional protection is a batch-request size cap (`rpc_batch_limit`), which is **disabled by default** and does not apply to individual single-call requests. [4](#0-3) 

A grep across all `rpc/src/**/*.rs` files confirms zero uses of any rate-limiter type in the RPC layer, in contrast to the P2P relay layer (e.g., `sync/src/relayer/mod.rs`) and the hole-punching protocol, which both use `governor::RateLimiter` keyed by peer. [5](#0-4) 

### Impact Explanation

Each `send_alert` call with 4 crafted signatures (the maximum before `SigCountOverflow`) forces 4 secp256k1 ECDSA recoveries plus a Blake2b hash. An attacker sending requests in a tight loop can saturate one or more CPU cores on the target node, degrading block validation, transaction relay, and sync throughput — all of which share the same process. The node does not crash, but its ability to participate in consensus and serve legitimate RPC clients is impaired for the duration of the attack.

### Likelihood Explanation

The default `listen_address` is `127.0.0.1:8114`, which restricts access to localhost. However:

- The CKB documentation explicitly warns that operators sometimes expose the port to external machines.
- Optional TCP (`tcp_listen_address`) and WebSocket (`ws_listen_address`) endpoints can be bound to `0.0.0.0`.
- The bounty scope explicitly lists "RPC caller" as a valid unprivileged attacker profile.
- No authentication is required to call `send_alert`; the only credential checked is the alert's embedded signatures, which are verified *after* the expensive work is done.
- The attack requires only a standard HTTP POST loop — no special tooling. [6](#0-5) 

### Recommendation

1. Add a per-IP (or global) rate limiter on `send_alert` at the RPC handler level, analogous to the `governor::RateLimiter` already used in the relay and hole-punching protocols.
2. Enforce a hard cap on the number of signatures accepted in a single `Alert` payload before any cryptographic work begins (currently the cap is implicitly `pks.len()` but is enforced only inside `verify_m_of_n` after parsing).
3. Consider moving the `SigCountOverflow` check to `verify_signatures` before any `Signature::from_slice` calls.

### Proof of Concept

```bash
# Craft an alert with 4 dummy 65-byte signatures and a far-future notice_until.
# Repeat in a tight loop to saturate CPU on the target node.
while true; do
  curl -s -X POST http://<node>:8114/ \
    -H 'Content-Type: application/json' \
    -d '{
      "jsonrpc":"2.0","id":1,"method":"send_alert",
      "params":[{
        "id":"0x1","cancel":"0x0","priority":"0x1",
        "message":"x",
        "notice_until":"0xffffffffffff",
        "signatures":[
          "0xbd07059aa9a3d057da294c2c4d96fa1e67eeb089837c87b523f124239e18e9fc7d11bb95b720478f7f937d073517d0e4eb9a91d12da5c88a05f750362f4c214dd00",
          "0xbd07059aa9a3d057da294c2c4d96fa1e67eeb089837c87b523f124239e18e9fc7d11bb95b720478f7f937d073517d0e4eb9a91d12da5c88a05f750362f4c214dd01",
          "0xbd07059aa9a3d057da294c2c4d96fa1e67eeb089837c87b523f124239e18e9fc7d11bb95b720478f7f937d073517d0e4eb9a91d12da5c88a05f750362f4c214dd02",
          "0xbd07059aa9a3d057da294c2c4d96fa1e67eeb089837c87b523f124239e18e9fc7d11bb95b720478f7f937d073517d0e4eb9a91d12da5c88a05f750362f4c214dd03"
        ]
      }]
    }' > /dev/null
done
```

Each iteration forces `calc_alert_hash()` + up to 4 `sig.recover()` calls with no throttle. The node returns `AlertFailedToVerifySignatures` on each call but performs the full cryptographic work before doing so. [2](#0-1) [7](#0-6)

### Citations

**File:** rpc/src/module/alert.rs (L102-131)
```rust
    fn send_alert(&self, alert: Alert) -> Result<()> {
        let alert: packed::Alert = alert.into();
        let now_ms = ckb_systemtime::unix_time_as_millis();
        let notice_until: u64 = alert.raw().notice_until().into();
        if notice_until < now_ms {
            return Err(RPCError::invalid_params(format!(
                "Expected `params[0].notice_until` in the future (> {now_ms}), got {notice_until}",
            )));
        }

        let result = self.verifier.verify_signatures(&alert);

        match result {
            Ok(()) => {
                // set self node notifier
                self.notifier.lock().add(&alert);

                self.network_controller.broadcast_with_handle(
                    SupportProtocols::Alert.protocol_id(),
                    alert.as_bytes(),
                    &self.handle,
                );
                Ok(())
            }
            Err(e) => Err(RPCError::custom_with_error(
                RPCError::AlertFailedToVerifySignatures,
                e,
            )),
        }
    }
```

**File:** util/network-alert/src/verifier.rs (L33-64)
```rust
    pub fn verify_signatures(&self, alert: &packed::Alert) -> Result<(), AnyError> {
        trace!("Verifying alert {:?}", alert);
        let message = Message::from_slice(alert.calc_alert_hash().as_slice())?;
        let signatures: Vec<Signature> = alert
            .signatures()
            .into_iter()
            .filter_map(
                |sig_data| match Signature::from_slice(sig_data.as_reader().raw_data()) {
                    Ok(sig) => {
                        if sig.is_valid() {
                            Some(sig)
                        } else {
                            debug!("invalid signature: {:?}", sig);
                            None
                        }
                    }
                    Err(err) => {
                        debug!("signature error: {}", err);
                        None
                    }
                },
            )
            .collect();
        verify_m_of_n(
            &message,
            self.config.signatures_threshold,
            &signatures,
            &self.pubkeys,
        )
        .map_err(|err| err.kind())?;
        Ok(())
    }
```

**File:** util/multisig/src/secp256k1.rs (L11-54)
```rust
pub fn verify_m_of_n<S>(
    message: &Message,
    m_threshold: usize,
    sigs: &[Signature],
    pks: &HashSet<Pubkey, S>,
) -> Result<(), Error>
where
    S: BuildHasher,
{
    if sigs.len() > pks.len() {
        return Err(ErrorKind::SigCountOverflow.into());
    }
    if m_threshold > sigs.len() {
        return Err(ErrorKind::SigNotEnough.into());
    }

    let mut used_pks: HashSet<Pubkey> = HashSet::with_capacity(m_threshold);
    let verified_sig_count = sigs
        .iter()
        .filter_map(|sig| {
            trace!(
                "Recover sig {:x?} with message {:x?}",
                &sig.serialize()[..],
                message.as_ref()
            );
            match sig.recover(message) {
                Ok(pubkey) => Some(pubkey),
                Err(err) => {
                    debug!("recover secp256k1 sig error: {}", err);
                    None
                }
            }
        })
        .filter(|rec_pk| pks.contains(rec_pk) && used_pks.insert(rec_pk.to_owned()))
        .take(m_threshold)
        .count();
    if verified_sig_count < m_threshold {
        return Err(ErrorKind::Threshold {
            pass_sigs: verified_sig_count,
            threshold: m_threshold,
        }
        .into());
    }
    Ok(())
```

**File:** rpc/src/server.rs (L119-129)
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

**File:** sync/src/relayer/mod.rs (L89-99)
```rust
        // setup a rate limiter keyed by peer and message type that lets through 30 requests per second
        // current max rps is 10 (ASK_FOR_TXS_TOKEN / TX_PROPOSAL_TOKEN), 30 is a flexible hard cap with buffer
        let quota = governor::Quota::per_second(std::num::NonZeroU32::new(30).unwrap());
        let rate_limiter = RateLimiter::hashmap(quota);

        Relayer {
            chain,
            shared,
            rate_limiter,
        }
    }
```
