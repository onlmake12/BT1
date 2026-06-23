### Title
Any RPC Caller Can Remove Any Pending Transaction Without Ownership Verification - (File: `rpc/src/module/pool.rs`)

### Summary
The `remove_transaction` RPC method in `PoolRpcImpl` accepts any transaction hash and unconditionally removes the matching entry from the transaction pool, with no check that the caller has any relationship to or ownership of the transaction. This is a direct access-control analog to the GNTDeposit `withdraw` bug: just as any Ethereum user could call `withdraw` to burn another party's tokens, any local RPC caller can silently evict any other user's pending transaction from the CKB mempool, causing transaction censorship without the submitter's consent.

### Finding Description
`remove_transaction` is implemented at `rpc/src/module/pool.rs` lines 662–669:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

The function takes a caller-supplied `tx_hash` and forwards it directly to `remove_local_tx`. There is no check that the caller submitted the transaction, owns any of its inputs, or has any other authorization relationship to it. The underlying `remove_tx` implementation in `tx-pool/src/process.rs` (lines 440–455) removes the entry from the verify queue, the orphan pool, and the main pool map — all three stages — based solely on the hash.

The same pattern applies to `clear_tx_pool` (lines 684–692) and `clear_tx_verify_queue` (lines 694–700), which allow any caller to wipe the entire pool or the entire verify queue with a single unauthenticated call.

The RPC server has no per-method authentication layer. The only protection is that the default `listen_address` is `127.0.0.1:8114` (localhost). Any process running on the same host — including a malicious co-tenant process, a compromised application, or a script run by a local user — can reach this endpoint without credentials and invoke `remove_transaction` against any hash it observes (e.g., by polling `get_raw_tx_pool`).

The `send_alert` RPC, by contrast, correctly enforces a 2-of-4 multi-signature check before accepting the call, demonstrating that the codebase does implement privileged-action guards where the designers intended them. No equivalent guard exists for `remove_transaction`.

### Impact Explanation
An attacker with local RPC access can:
1. Poll `get_raw_tx_pool` to enumerate all pending transaction hashes.
2. Call `remove_transaction` for any or all of them.
3. The targeted transactions are silently dropped from the pool. Their submitters receive no notification; the transactions simply disappear and must be resubmitted (paying fees again) or will never confirm.

This enables targeted transaction censorship: an attacker can repeatedly evict a specific transaction every time it is resubmitted, permanently preventing it from being included in a block. It also enables griefing of fee-paying users and disruption of time-sensitive operations (e.g., DAO withdrawals, contract interactions with deadlines). Using `clear_tx_pool`, the attacker can wipe the entire mempool, causing a denial-of-service for all pending transactions on the node.

### Likelihood Explanation
The attacker profile is "supported local CLI/RPC user," which the prompt explicitly lists as in scope. No privileged key, leaked credential, or network access beyond localhost is required. The RPC port is unauthenticated by design. Any co-located process (another user account, a compromised service, a malicious dependency) can reach `127.0.0.1:8114` and issue the call. The transaction hashes needed to target specific transactions are publicly visible via `get_raw_tx_pool`. The attack is trivially scriptable and repeatable.

### Recommendation
Add an ownership or authorization check to `remove_transaction`. The simplest correct fix is to restrict the method so that only the node operator (authenticated via a token or a separate privileged RPC socket) can invoke it, mirroring the multi-signature guard on `send_alert`. Alternatively, record the submitter identity (e.g., a session token or local process credential) at submission time and require the caller to present the same identity when requesting removal. At minimum, `clear_tx_pool` and `clear_tx_verify_queue` should be moved to a privileged-only module (e.g., `Debug` or a new `Admin` module) that is disabled by default in production configurations.

### Proof of Concept
1. Node is running with default config (`listen_address = "127.0.0.1:8114"`, `modules` includes `"Pool"`).
2. Victim submits a transaction via `send_transaction`; it enters the pending pool.
3. Attacker (any local process) calls:
   ```
   curl -X POST http://127.0.0.1:8114 \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":1}'
   ```
   to obtain the victim's `tx_hash`.
4. Attacker calls:
   ```
   curl -X POST http://127.0.0.1:8114 \
     -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["<tx_hash>"],"id":2}'
   ```
5. Response: `{"result": true, ...}`. The victim's transaction is gone from the pool with no error or notification to the submitter. Repeating steps 3–4 after each resubmission permanently censors the transaction. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
