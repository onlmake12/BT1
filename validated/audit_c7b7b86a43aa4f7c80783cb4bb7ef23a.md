### Title
Fee-on-Transfer Token Bypass in `transfer_fee` Allows Underpayment of Price Update Fees — (`target_chains/starknet/contracts/src/pyth.cairo`)

---

### Summary

The `transfer_fee` helper in the Pyth Starknet contract validates the `transferFrom` return value but never verifies that the contract's actual token balance increased by the expected `total_fee`. If the configured fee token implements a fee-on-transfer mechanism, the contract accepts less than the required fee while treating the update as fully paid, allowing any caller to update price feeds at a discount.

---

### Finding Description

In `target_chains/starknet/contracts/src/pyth.cairo`, the private `transfer_fee` function is responsible for collecting the price-update fee from the caller:

```cairo
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
``` [1](#0-0) 

The function checks:
1. That the caller has approved at least `total_fee`.
2. That the caller's balance is at least `total_fee`.
3. That `transferFrom` returns `true`.

It does **not** check that the contract's balance of `fee_token` actually increased by `total_fee` after the call. For a fee-on-transfer token (one that silently deducts a percentage during every transfer), `transferFrom` returns `true` and the caller's balance decreases by `total_fee`, but the contract only receives `total_fee * (1 - fee_rate)`. The function still returns `true`, and `update_price_feeds_internal` proceeds as if the full fee was paid. [2](#0-1) 

This function is invoked by every public price-update entry point:
- `update_price_feeds` [3](#0-2) 
- `parse_price_feed_updates` [4](#0-3) 
- `parse_unique_price_feed_updates` [5](#0-4) 
- `update_price_feeds_if_necessary` [6](#0-5) 

The constructor comment explicitly states that the fee token is open-ended: *"Any other ERC20-compatible token can also be used."* [7](#0-6) 

The contract stores two configurable fee token addresses (`fee_token_address1`, `fee_token_address2`) and their corresponding per-update fees. [8](#0-7) 

---

### Impact Explanation

When a fee-on-transfer token is configured as `fee_token_address1` or `fee_token_address2`:

- Every caller of `update_price_feeds` (or any of the three sibling entry points) pays the nominal `total_fee` from their wallet but the Pyth contract receives only `total_fee × (1 − fee_rate)`.
- The contract treats the update as fully paid and writes the new price data to storage.
- The protocol's fee revenue is permanently reduced proportionally to the token's transfer fee rate.
- Because the contract has no internal accounting ledger for collected fees (no `accruedFees` variable), the shortfall is invisible on-chain and cannot be detected or corrected without an external balance audit.
- At scale (many updates per block across many callers), the cumulative revenue loss can be material.

---

### Likelihood Explanation

The fee token is set at deployment and can be updated via governance (`execute_governance_instruction`). The constructor documentation explicitly permits any ERC20-compatible token. Tokens with latent fee-on-transfer capability (e.g., USDT, which has the mechanism in its contract but currently sets the rate to zero) are widely used on Starknet. If governance ever sets such a token, or if the token's fee rate is later activated by its own owner, the vulnerability becomes immediately exploitable by any unprivileged caller of `update_price_feeds` — no special access is required beyond having the fee token and valid price update data.

---

### Recommendation

Add a balance-delta check inside `transfer_fee` to verify that the contract actually received the expected amount:

```cairo
fn transfer_fee(
    num_updates: u8,
    caller: ContractAddress,
    contract: ContractAddress,
    fee_token: ContractAddress,
    single_update_fee: u256,
) -> bool {
    let total_fee = single_update_fee * num_updates.into();
    let fee_contract = IERC20CamelDispatcher { contract_address: fee_token };
    if fee_contract.allowance(caller, contract) < total_fee {
        return false;
    }
    if fee_contract.balanceOf(caller) < total_fee {
        return false;
    }
    let balance_before = fee_contract.balanceOf(contract);
    if !fee_contract.transferFrom(caller, contract, total_fee) {
        return false;
    }
    let balance_after = fee_contract.balanceOf(contract);
    // Enforce that the contract received exactly total_fee
    balance_after - balance_before >= total_fee
}
```

This mirrors the standard "balance-before / balance-after" pattern recommended for fee-on-transfer token safety.

---

### Proof of Concept

1. Deploy a StarkNet ERC20 token that deducts 10% on every `transferFrom` call (fee-on-transfer token).
2. Deploy the Pyth contract with this token as `fee_token_address1` and `single_update_fee1 = 1000`.
3. Approve the Pyth contract for `1000` tokens and call `update_price_feeds` with a valid 1-update payload.
4. `transfer_fee` checks: allowance ≥ 1000 ✓, balance ≥ 1000 ✓, `transferFrom` returns `true` ✓ → returns `true`.
5. The Pyth contract's balance increases by only `900` (10% fee taken by the token).
6. The price feed is updated successfully despite the protocol receiving only 90% of the required fee.
7. Repeat at scale: for every 10 updates, the protocol loses the equivalent of 1 full update's fee revenue.

### Citations

**File:** target_chains/starknet/contracts/src/pyth.cairo (L126-129)
```text
        fee_token_address1: ContractAddress,
        fee_token_address2: ContractAddress,
        single_update_fee1: u256,
        single_update_fee2: u256,
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L146-151)
```text
    ///
    /// `fee_token_address1` is the address of the ERC20 token used to pay fees to Pyth
    /// for price updates. There is no native token on Starknet so an ERC20 contract has to be used.
    /// On Devnet, an ETH fee contract is pre-deployed. On Starknet testnet, ETH and STRK fee tokens
    /// are available. Any other ERC20-compatible token can also be used.
    /// In a Starknet Forge testing environment, a fee contract must be deployed manually.
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L289-291)
```text
        fn update_price_feeds(ref self: ContractState, data: ByteBuffer) {
            self.update_price_feeds_internal(data, array![], 0, 0, false);
        }
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L310-330)
```text
        fn update_price_feeds_if_necessary(
            ref self: ContractState,
            update: ByteBuffer,
            required_publish_times: Array<PriceFeedPublishTime>,
        ) {
            let mut i = 0;
            let mut found = false;
            while i < required_publish_times.len() {
                let item = required_publish_times.at(i);
                let latest_time = self.latest_price_info.entry(*item.price_id).read().publish_time;
                if latest_time < *item.publish_time {
                    self.update_price_feeds(update);
                    found = true;
                    break;
                }
                i += 1;
            }
            if !found {
                panic_with_felt252(UpdatePriceFeedsIfNecessaryError::NoFreshUpdate.into());
            }
        }
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L332-343)
```text
        fn parse_price_feed_updates(
            ref self: ContractState,
            data: ByteBuffer,
            price_ids: Array<u256>,
            min_publish_time: u64,
            max_publish_time: u64,
        ) -> Array<PriceFeed> {
            self
                .update_price_feeds_internal(
                    data, price_ids, min_publish_time, max_publish_time, false,
                )
        }
```

**File:** target_chains/starknet/contracts/src/pyth.cairo (L345-351)
```text
        fn parse_unique_price_feed_updates(
            ref self: ContractState,
            data: ByteBuffer,
            price_ids: Array<u256>,
            publish_time: u64,
            max_staleness: u64,
        ) -> Array<PriceFeed> {
```

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
