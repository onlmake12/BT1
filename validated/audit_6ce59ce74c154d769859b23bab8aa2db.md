### Title
Fee Charged for Stale (Non-Fresh) Price Updates With No Withdrawal Path — (`target_chains/sui/contracts/sources/pyth.move`, `price_info.move`)

---

### Summary

`update_single_price_feed` unconditionally deposits the caller's fee into the `PriceInfoObject` **before** performing the freshness check. When the submitted update carries a `publish_time` equal to the cached timestamp, `is_fresh_update` returns `false`, no state change occurs, no `PriceFeedUpdateEvent` is emitted, but the fee is permanently locked inside the `PriceInfoObject`. No withdrawal function exists anywhere in the contract suite, and the `fee_recipient` mechanism is explicitly marked as no longer in use.

---

### Finding Description

**Step 1 — Fee deposited unconditionally.**

In `update_single_price_feed`, the fee is deposited before any freshness check:

```
assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);
price_info::deposit_fee_coins(price_info_object, fee);   // ← fee locked here
...
update_cache(latest_only, cur_price_info, price_info_object, clock);
``` [1](#0-0) 

**Step 2 — Freshness check uses strict `>`.**

`is_fresh_update` returns `false` when `update_timestamp == cached_timestamp`:

```move
update_timestamp > cached_timestamp
``` [2](#0-1) 

**Step 3 — Stale path: no state change, no event.**

`update_cache` silently does nothing when `is_fresh_update` is `false`:

```move
if (is_fresh_update(update, price_info_object)){
    pyth_event::emit_price_feed_update(...);
    price_info::update_price_info_object(...);
}
// else: nothing — fee already deposited
``` [3](#0-2) 

**Step 4 — `deposit_fee_coins` is `public` and one-way.**

The function only ever joins coins into the dynamic field; there is no corresponding `withdraw_fee_coins` or `collect_fee` function anywhere in the contract suite: [4](#0-3) 

**Step 5 — `fee_recipient` mechanism is explicitly dead.**

`set_fee_recipient.move` opens with the comment: *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore."* The `fee_recipient_address` field in `State` is stored but never used to transfer coins out of any `PriceInfoObject`. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

Every call to `update_single_price_feed` where `publish_time == cached_timestamp` results in:
- The caller's SUI fee permanently locked inside the `PriceInfoObject`.
- No `PriceFeedUpdateEvent` emitted.
- No price state change.
- No revert — the transaction succeeds silently.

Because `PriceInfoObject` is a shared Sui object and `deposit_fee_coins` is `public`, the locked balance can only grow. There is no on-chain path to recover these funds.

---

### Likelihood Explanation

This is reachable in normal operation. Two independent updaters submitting the same VAA/accumulator message in the same epoch will both pay the fee; the second one gets no update and loses their SUI. It is also trivially reproducible by any single caller who submits the same price update twice.

---

### Recommendation

Move the fee deposit **after** the freshness check, or abort (revert) the transaction when `is_fresh_update` returns `false`, refunding the caller. The Aptos implementation correctly counts only successful updates before charging:

```move
// Aptos pattern (pyth.move Aptos):
total_updates = total_updates + update_price_feed_from_single_vaa(...)
let update_fee = state::get_base_update_fee() * total_updates;
``` [7](#0-6) 

For Sui, the fix is:

```move
// Only deposit fee if the update is actually fresh
if (is_fresh_update(cur_price_info, price_info_object)) {
    price_info::deposit_fee_coins(price_info_object, fee);
    update_cache(...);
} else {
    abort E_STALE_PRICE_UPDATE
}
```

---

### Proof of Concept

```move
#[test]
fun test_fee_locked_on_stale_update() {
    // 1. Setup: create PriceInfoObject with publish_time = T
    // 2. Call update_single_price_feed with publish_time = T (same) → fee deposited, no event
    // 3. Call update_single_price_feed again with publish_time = T → fee deposited again, no event
    // Assert: balance in PriceInfoObject == 2 * base_update_fee
    // Assert: PriceFeedUpdateEvent emitted count == 0 (or 1 if first call was fresh)
    // Assert: cached price unchanged
    assert!(price_info::get_balance(&price_info_object) == 2 * DEFAULT_BASE_UPDATE_FEE, 0);
}
```

The existing test at line 817 already confirms the fee accumulation pattern: [8](#0-7)

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-287)
```text
        assert!(state::get_base_update_fee(pyth_state) <= coin::value(&fee), E_INSUFFICIENT_FEE);

        // store fee coins within price info object
        price_info::deposit_fee_coins(price_info_object, fee);

        // Find price update corresponding to PriceInfoObject within the array of price_updates
        // and use it to update PriceInfoObject.
        let i = 0;
        let found = false;
        while (i < hot_potato_vector::length<PriceInfo>(&price_updates)){
            let cur_price_info = hot_potato_vector::borrow<PriceInfo>(&price_updates, i);
            if (has_same_price_identifier(cur_price_info, price_info_object)){
                found = true;
                update_cache(latest_only, cur_price_info, price_info_object, clock);
```

**File:** target_chains/sui/contracts/sources/pyth.move (L316-323)
```text
        if (is_fresh_update(update, price_info_object)){
            pyth_event::emit_price_feed_update(price_feed::from(price_info::get_price_feed(update)), clock::timestamp_ms(clock)/1000);
            price_info::update_price_info_object(
                price_info_object,
                update
            );
        }
    }
```

**File:** target_chains/sui/contracts/sources/pyth.move (L327-338)
```text
    fun is_fresh_update(update: &PriceInfo, price_info_object: &PriceInfoObject): bool {
        // Get the timestamp of the update's current price
        let price_feed = price_info::get_price_feed(update);
        let update_timestamp = price::get_timestamp(&price_feed::get_price(price_feed));

        // Get the timestamp of the cached data for the price identifier
        let cached_price_info = price_info::get_price_info_from_price_info_object(price_info_object);
        let cached_price_feed =  price_info::get_price_feed(&cached_price_info);
        let cached_timestamp = price::get_timestamp(&price_feed::get_price(cached_price_feed));

        update_timestamp > cached_timestamp
    }
```

**File:** target_chains/sui/contracts/sources/pyth.move (L816-818)
```text
        // check fee coins are deposited in the price info object
        assert!(price_info::get_balance(&price_info_object_1)==DEFAULT_BASE_UPDATE_FEE, 0);

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

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-3)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
module pyth::set_fee_recipient {
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

**File:** target_chains/aptos/contracts/sources/pyth.move (L148-157)
```text
        let total_updates = 0;
        // Update the price feed from each VAA
        while (!vector::is_empty(&vaas)) {
            total_updates = total_updates + update_price_feed_from_single_vaa(vector::pop_back(&mut vaas));
        };
        // Charge the message update fee
        let update_fee = state::get_base_update_fee() * total_updates;
        let fee = coin::withdraw<AptosCoin>(account, update_fee);
        coin::deposit(@pyth, fee);
    }
```
