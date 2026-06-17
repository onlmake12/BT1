### Title
Unconditional Zero-Amount ERC20 `transferFrom` in Starknet `transfer_fee` Causes DoS on `update_price_feeds` When Fee Is Set to Zero — (`target_chains/starknet/contracts/src/pyth.cairo`)

---

### Summary

The Starknet Pyth contract's internal `transfer_fee` function calls `transferFrom` unconditionally, even when the computed `total_fee` is zero. If governance sets either `single_update_fee1` or `single_update_fee2` to zero, and the configured fee token is an ERC20 that reverts on zero-value transfers, every call to `update_price_feeds` on Starknet will revert, rendering price feed updates permanently unavailable until the fee is changed.

---

### Finding Description

In `target_chains/starknet/contracts/src/pyth.cairo`, the free-standing `transfer_fee` function computes `total_fee = single_update_fee * num_updates` and then unconditionally calls `fee_contract.transferFrom(caller, contract, total_fee)` without first checking whether `total_fee > 0`: [1](#0-0) 

When `single_update_fee1` (or `single_update_fee2`) is zero, `total_fee` evaluates to `0`. The two preceding guard checks — `allowance < total_fee` and `balanceOf < total_fee` — both evaluate to `false` (since `u256` cannot be negative, `x < 0` is always false), so neither guard short-circuits the function. Execution falls through to `transferFrom(caller, contract, 0)`.

The fee is set via the governance-controlled `set_fee` function, which accepts `value = 0` and `expo = 0` without restriction: [2](#0-1) 

`apply_decimal_expo(0, 0)` returns `0`, so governance can legitimately set either fee token's per-update fee to zero.

The caller path is `update_price_feeds_internal`, which calls `transfer_fee` for both configured fee tokens: [3](#0-2) 

---

### Impact Explanation

If the fee is set to zero and the configured ERC20 fee token (STRK or ETH on Starknet) reverts on zero-value `transferFrom` calls, every invocation of `update_price_feeds` (and all wrappers that call `update_price_feeds_internal`) will revert. This is a complete DoS on Pyth price feed updates on Starknet — no user can update any price feed until governance changes the fee to a non-zero value or changes the fee token.

---

### Likelihood Explanation

Governance can set the fee to zero via a valid governance message (e.g., to make price updates free). This is a realistic operational scenario. The Starknet documentation itself notes the fee is currently set to the minimum possible value (1 WEI), implying it is expected to be adjustable. If the fee is ever set to zero and the fee token has standard zero-transfer revert behavior, the DoS is immediate and affects all users.

---

### Recommendation

Add a zero-amount guard at the top of `transfer_fee` before calling `transferFrom`:

```cairo
fn transfer_fee(
    num_updates: u8,
    caller: ContractAddress,
    contract: ContractAddress,
    fee_token: ContractAddress,
    single_update_fee1: u256,
) -> bool {
    let total_fee = single_update_fee1 * num_updates.into();
    if total_fee == 0 {
        return true;  // No fee required; skip transfer
    }
    let fee_contract = IERC20CamelDispatcher { contract_address: fee_token };
    if fee_contract.allowance(caller, contract) < total_fee {
        return false;
    }
    if fee_contract.balanceOf(caller) < total_fee {
        return false;
    }
    if !fee_contract.transferFrom(caller, contract, total_fee) {
        return false;
    }
    true
}
```

---

### Proof of Concept

1. Governance submits a valid governance VAA calling `set_fee(value=0, expo=0, token=STRK_address)`. This sets `single_update_fee1 = 0`.
2. Any user calls `update_price_feeds(data)` with valid accumulator update data containing `num_updates = N > 0`.
3. Inside `update_price_feeds_internal`, `transfer_fee(N, caller, contract, STRK_address, 0)` is called.
4. `total_fee = 0 * N = 0`. Both allowance and balance checks pass (since `x < 0` is always false for `u256`).
5. `STRK_token.transferFrom(caller, contract, 0)` is called. If STRK (or any configured fee token) reverts on zero-value transfers, the entire transaction reverts.
6. All subsequent `update_price_feeds` calls revert identically — price feed updates are completely unavailable on Starknet. [4](#0-3)

### Citations

**File:** target_chains/starknet/contracts/src/pyth.cairo (L661-679)
```text
            let fee1_transfered = transfer_fee(
                num_updates,
                caller,
                contract,
                self.fee_token_address1.read(),
                self.single_update_fee1.read(),
            );
            if !fee1_transfered {
                let fee2_transfered = transfer_fee(
                    num_updates,
                    caller,
                    contract,
                    self.fee_token_address2.read(),
                    self.single_update_fee2.read(),
                );
                if !fee2_transfered {
                    panic_with_felt252(UpdatePriceFeedsError::InsufficientFeeAllowance.into());
                }
            }
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L729-744)
```text
        fn set_fee(ref self: ContractState, value: u64, expo: u64, token: ContractAddress) {
            let new_fee = apply_decimal_expo(value, expo);
            let old_fee = if token == self.fee_token_address1.read() {
                let old_fee = self.single_update_fee1.read();
                self.single_update_fee1.write(new_fee);
                old_fee
            } else if token == self.fee_token_address2.read() {
                let old_fee = self.single_update_fee2.read();
                self.single_update_fee2.write(new_fee);
                old_fee
            } else {
                panic_with_felt252(GovernanceActionError::InvalidGovernanceMessage.into())
            };
            let event = FeeSet { old_fee, new_fee, token };
            self.emit(event);
        }
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L782-801)
```text
    fn transfer_fee(
        num_updates: u8,
        caller: ContractAddress,
        contract: ContractAddress,
        fee_token: ContractAddress,
        single_update_fee1: u256,
    ) -> bool {
        let total_fee = single_update_fee1 * num_updates.into();
        let fee_contract = IERC20CamelDispatcher { contract_address: fee_token };
        if fee_contract.allowance(caller, contract) < total_fee {
            return false;
        }
        if fee_contract.balanceOf(caller) < total_fee {
            return false;
        }
        if !fee_contract.transferFrom(caller, contract, total_fee) {
            return false;
        }
        true
    }
```
