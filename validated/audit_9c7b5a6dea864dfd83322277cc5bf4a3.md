### Title
Update Fees Permanently Locked in `PriceInfoObject` Instead of Being Forwarded to `fee_recipient_address` - (File: `target_chains/sui/contracts/sources/pyth.move`)

---

### Summary

Every call to `update_single_price_feed` deposits the user-paid SUI fee into the `PriceInfoObject` shared object via `price_info::deposit_fee_coins`. The `State` object maintains a `fee_recipient_address` field specifically for receiving protocol fees, but it is never used during fee collection. No withdrawal function exists to extract accumulated fees from `PriceInfoObject`, making all collected protocol fees permanently locked and irrecoverable.

---

### Finding Description

In `pyth.move`, `update_single_price_feed` collects a fee from the caller and deposits it directly into the `PriceInfoObject`:

```move
// pyth.move line 274-277
assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);
// store fee coins within price info object
price_info::deposit_fee_coins(price_info_object, fee);
```

`deposit_fee_coins` in `price_info.move` stores the coins as a dynamic object field on the `PriceInfoObject` itself:

```move
// price_info.move lines 105-116
public fun deposit_fee_coins(price_info_object: &mut PriceInfoObject, fee_coins: Coin<SUI>) {
    if (!dynamic_object_field::exists_with_type<...>(&price_info_object.id, FEE_STORAGE_KEY)) {
        dynamic_object_field::add(&mut price_info_object.id, FEE_STORAGE_KEY, fee_coins);
    } else {
        let current_fee = dynamic_object_field::borrow_mut<...>(...);
        coin::join(current_fee, fee_coins);
    };
}
```

Meanwhile, `State` holds a `fee_recipient_address` field:

```move
// state.move line 48
fee_recipient_address: address,
```

This field is settable via governance (`set_fee_recipient.move`) and readable via `get_fee_recipient`, but it is **never consulted** during fee collection. The `set_fee_recipient.move` module itself acknowledges the disconnect with the comment: *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore."*

Critically, `price_info.move` exposes only `deposit_fee_coins` and `get_balance` — there is no `withdraw_fee_coins` or equivalent function. The `PriceInfoObject` is a shared Sui object with no privileged owner, so the accumulated SUI is permanently inaccessible.

---

### Impact Explanation

All SUI fees paid by users calling `update_single_price_feed` accumulate inside `PriceInfoObject` shared objects and can never be withdrawn. The `fee_recipient_address` governance parameter — which Pyth governance can set via VAA — has no effect on actual fee routing. Protocol revenue is permanently lost. The magnitude scales with usage: every price feed update across every price identifier contributes to the locked balance.

---

### Likelihood Explanation

This is triggered by every ordinary, unprivileged call to `update_single_price_feed`. No special role or condition is required. Any consumer of the Pyth price feed on Sui who pays the update fee causes this. The condition is always active on mainnet.

---

### Recommendation

Add a `withdraw_fee_coins` function (friend-gated to `pyth::pyth`) in `price_info.move` and route collected fees to `state::get_fee_recipient(pyth_state)` inside `update_single_price_feed`:

```diff
// pyth.move
- price_info::deposit_fee_coins(price_info_object, fee);
+ transfer::public_transfer(fee, state::get_fee_recipient(pyth_state));
```

Alternatively, if the intent is to accumulate fees in `PriceInfoObject`, add a privileged withdrawal path that transfers the balance to `fee_recipient_address`.

---

### Proof of Concept

1. User calls `update_single_price_feed(pyth_state, price_updates, &mut price_info_object_X, fee_coin, clock)`.
2. `fee_coin` (≥ `base_update_fee` SUI) is deposited into `price_info_object_X` via `deposit_fee_coins`.
3. `state::get_fee_recipient(pyth_state)` returns a valid address set by governance, but is never called.
4. Repeat for any number of updates — `price_info::get_balance(&price_info_object_X)` grows, but no code path exists to move those coins to the fee recipient.
5. The SUI is permanently locked inside the shared `PriceInfoObject`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-277)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);
```

**File:** target_chains/sui/contracts/sources/price_info.move (L105-116)
```text
    public fun deposit_fee_coins(price_info_object: &mut PriceInfoObject, fee_coins: Coin<SUI>) {
        if (!dynamic_object_field::exists_with_type<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY)) {
            dynamic_object_field::add(&mut price_info_object.id, FEE_STORAGE_KEY, fee_coins);
        }
        else {
            let current_fee = dynamic_object_field::borrow_mut<vector<u8>, Coin<SUI>>(
                &mut price_info_object.id,
                FEE_STORAGE_KEY
            );
            coin::join(current_fee, fee_coins);
        };
    }
```

**File:** target_chains/sui/contracts/sources/state.move (L43-54)
```text
    struct State has key, store {
        id: UID,
        governance_data_source: DataSource,
        stale_price_threshold: u64,
        base_update_fee: u64,
        fee_recipient_address: address,
        last_executed_governance_sequence: u64,
        consumed_vaas: ConsumedVAAs,

        // Upgrade capability.
        upgrade_cap: UpgradeCap
    }
```

**File:** target_chains/sui/contracts/sources/state.move (L119-121)
```text
    public fun get_fee_recipient(s: &State): address {
        s.fee_recipient_address
    }
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-3)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
module pyth::set_fee_recipient {
```
