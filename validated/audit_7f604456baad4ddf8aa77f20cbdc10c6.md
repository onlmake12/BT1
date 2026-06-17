The code tells a clear and complete story. Here is the verdict:

---

### Title
Collected Update Fees Are Permanently Locked in `PriceInfoObject` Dynamic Fields With No Withdrawal Path — (`target_chains/sui/contracts/sources/price_info.move`, `pyth.move`)

### Summary

Every call to `update_single_price_feed` deposits the caller's fee `Coin<SUI>` into the target `PriceInfoObject` via `deposit_fee_coins`. No function in the entire module set ever withdraws or transfers those coins to `fee_recipient_address`. The fees are permanently inaccessible.

### Finding Description

`update_single_price_feed` in `pyth.move` unconditionally calls `price_info::deposit_fee_coins(price_info_object, fee)`: [1](#0-0) 

`deposit_fee_coins` stores the coin as a dynamic object field under `FEE_STORAGE_KEY` on the `PriceInfoObject`: [2](#0-1) 

There is no `withdraw_fee_coins`, no `transfer::public_transfer` to `fee_recipient_address`, and no other egress path for these coins anywhere in the module. `get_fee_recipient` is a pure getter that is never used to route coins: [3](#0-2) 

The `set_fee_recipient.move` module contains an explicit admission that this mechanism is vestigial:

> "The previous version of the contract sent the fees to a recipient address but **this state is not used anymore**. This module is kept for backward compatibility." [4](#0-3) 

The governance action still allows setting `fee_recipient_address` in `State`: [5](#0-4) 

But that address is never the destination of any coin transfer. The `State` struct holds `fee_recipient_address` as a field: [6](#0-5) 

yet no code path ever calls `sui::transfer::public_transfer` or `coin::split`/`coin::join` targeting it.

### Impact Explanation

All SUI fees paid by every caller of `update_single_price_feed` accumulate inside individual `PriceInfoObject` dynamic fields and are permanently unrecoverable. The protocol's fee_recipient can never collect protocol revenue. The total locked amount grows monotonically with every price update across every price feed.

### Likelihood Explanation

This is not a conditional or edge-case path — it is the only code path. Every single fee payment goes through `deposit_fee_coins`. The code comment in `set_fee_recipient.move` confirms the withdrawal mechanism was deliberately removed. The likelihood is **certain** for any deployed instance of this contract.

### Recommendation

Add a privileged `withdraw_fee_coins` function (gated on `LatestOnly` or a governance capability) to `price_info.move` that extracts the stored `Coin<SUI>` from a `PriceInfoObject` and transfers it to `state::get_fee_recipient(pyth_state)`. Alternatively, route fees directly to the fee recipient at collection time inside `update_single_price_feed` using `sui::transfer::public_transfer(fee, state::get_fee_recipient(pyth_state))` instead of calling `deposit_fee_coins`.

### Proof of Concept

1. Deploy the contract with `base_update_fee = 50`.
2. Call `update_single_price_feed` N times across any `PriceInfoObject`s, each time passing `fee = coin::mint(50)`.
3. After each call, assert `price_info::get_balance(price_info_object) > 0` — it will equal `50 * (number of updates to that object)`.
4. Search the entire module set for any function that calls `sui::transfer::public_transfer` or `coin::split` targeting `state::get_fee_recipient(...)` — none exists.
5. Assert that `state::get_fee_recipient(pyth_state)` has received zero SUI from protocol fees — confirmed.

The total fees locked = `base_update_fee × total_update_calls`, permanently inaccessible to the fee recipient.

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

**File:** target_chains/sui/contracts/sources/state.move (L196-202)
```text
    public(friend) fun set_fee_recipient(
        _: &LatestOnly,
        self: &mut State,
        addr: address
    ) {
        self.fee_recipient_address = addr;
    }
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-3)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
module pyth::set_fee_recipient {
```
