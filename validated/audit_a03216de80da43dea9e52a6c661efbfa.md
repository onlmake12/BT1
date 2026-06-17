### Title
`update_contract` Not Marked `#[payable]` Causes Upgrade to Always Fail on Storage Increase — (`File: target_chains/near/receiver/src/governance.rs`)

### Summary
The `update_contract` function in the NEAR Pyth receiver contract is not annotated with `#[payable]`, yet it captures `env::attached_deposit()` and forwards it to the `refund_upgrade` callback to cover storage cost increases from deploying new contract code. Because NEAR SDK automatically rejects any non-zero deposit on a non-payable function, `env::attached_deposit()` is always zero inside `update_contract`. When the new contract binary is larger than the old one (the common case for upgrades), `refund_storage_usage` computes a positive storage cost and then fails with `InsufficientDeposit`, permanently blocking the upgrade.

### Finding Description
`update_contract` is the publicly callable entry point for self-upgrading the NEAR Pyth receiver contract after governance has approved a new code hash: [1](#0-0) 

It delegates to `upgrade`, which captures `env::attached_deposit()` and schedules it as the `amount` argument to the `refund_upgrade` callback: [2](#0-1) 

`refund_upgrade` then calls `refund_storage_usage` with that amount: [3](#0-2) 

`refund_storage_usage` computes the byte cost of any storage increase and subtracts it from `deposit`. When `deposit` is 0 and storage grew, `deposit.checked_sub(cost)` returns `None`, returning `Err(InsufficientDeposit)`: [4](#0-3) 

Because `update_contract` lacks `#[payable]`, the NEAR runtime panics on any transaction that attaches a non-zero deposit, so callers have no way to supply the required funds.

### Impact Explanation
Any governance-approved contract upgrade whose new binary is larger than the currently deployed binary is permanently unexecutable. Since real-world upgrades almost always add code (bug fixes, new features), this effectively freezes the contract at its current version. The Pyth team cannot patch critical vulnerabilities or ship improvements to the on-chain NEAR receiver.

### Likelihood Explanation
The failure is deterministic: every upgrade attempt where `storage_after > storage_before` will revert with `InsufficientDeposit`. Contract binaries grow with virtually every non-trivial change. The condition is therefore expected to trigger on the very first real upgrade attempt.

### Recommendation
Add `#[payable]` to `update_contract`:

```rust
#[payable]
#[handle_result]
pub fn update_contract(&mut self) -> Result<Promise, Error> {
    env::setup_panic_hook();
    let new_code = env::input().unwrap();
    self.upgrade(new_code)
}
```

This mirrors the fix applied in the referenced report (`withdraw_minter` → `#[payable]`) and is the standard NEAR SDK pattern for any function that reads `env::attached_deposit()` for fee or storage accounting.

### Proof of Concept
1. Governance submits a `SetUpgradeHash` VAA setting `codehash` to `sha256(new_code)` via `execute_governance_instruction`.
2. Any unprivileged account calls `update_contract` and submits `new_code` (which is 1 byte larger than the current binary).
3. NEAR runtime sees `#[payable]` is absent; if the caller attaches any deposit the call panics immediately. If they attach 0, execution proceeds.
4. `upgrade` is called; `env::attached_deposit()` = 0 is forwarded to `refund_upgrade`.
5. After `deploy_contract` executes, `env::storage_usage()` increases by at least 1 storage unit.
6. `refund_storage_usage(account, storage_before, storage_after, NearToken(0), None)` computes `cost = byte_cost * diff > 0`, then `NearToken(0).checked_sub(cost) = None` → `Err(InsufficientDeposit)`.
7. The upgrade callback fails; the upgrade is rolled back. The contract remains on the old code indefinitely. [1](#0-0) [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/near/receiver/src/governance.rs (L465-470)
```rust
    #[handle_result]
    pub fn update_contract(&mut self) -> Result<Promise, Error> {
        env::setup_panic_hook();
        let new_code = env::input().unwrap();
        self.upgrade(new_code)
    }
```

**File:** target_chains/near/receiver/src/governance.rs (L472-486)
```rust
    fn upgrade(&mut self, new_code: Vec<u8>) -> Result<Promise, Error> {
        let signature = TryInto::<[u8; 32]>::try_into(env::sha256(&new_code)).unwrap();
        let default = <[u8; 32] as Default>::default();
        ensure!(signature != default, UnauthorizedUpgrade);
        ensure!(signature == self.codehash, UnauthorizedUpgrade);

        Ok(Promise::new(env::current_account_id())
            .deploy_contract(new_code)
            .then(Self::ext(env::current_account_id()).migrate())
            .then(Self::ext(env::current_account_id()).refund_upgrade(
                env::predecessor_account_id(),
                env::attached_deposit(),
                env::storage_usage(),
            )))
    }
```

**File:** target_chains/near/receiver/src/governance.rs (L492-499)
```rust
    pub fn refund_upgrade(
        &mut self,
        account_id: AccountId,
        amount: NearToken,
        storage: u64,
    ) -> Result<(), Error> {
        Self::refund_storage_usage(account_id, storage, env::storage_usage(), amount, None)
    }
```

**File:** target_chains/near/receiver/src/lib.rs (L662-703)
```rust
    fn refund_storage_usage(
        recipient: AccountId,
        before: StorageUsage,
        after: StorageUsage,
        deposit: NearToken,
        additional_fee: Option<NearToken>,
    ) -> Result<(), Error> {
        let fee = additional_fee.unwrap_or_default();

        if let Some(diff) = after.checked_sub(before) {
            // Handle storage increases if checked_sub succeeds.
            let cost = (env::storage_byte_cost().checked_mul(diff.into()).unwrap())
                .checked_add(fee)
                .unwrap();

            // Use match
            match deposit.checked_sub(cost) {
                Some(refund) => {
                    if !refund.is_zero() {
                        Promise::new(recipient).transfer(refund);
                    }
                }
                None => {
                    return Err(Error::InsufficientDeposit);
                }
            }
        } else {
            // If checked_sub fails we have a storage decrease, we want to refund them the cost of
            // the amount reduced, as well the original deposit they sent.
            let storage_refund = env::storage_byte_cost()
                .checked_mul(before.checked_sub(after).unwrap().into())
                .unwrap();
            let refund = storage_refund
                .checked_add(deposit)
                .unwrap()
                .checked_sub(fee)
                .unwrap();
            Promise::new(recipient).transfer(refund);
        }

        Ok(())
    }
```
