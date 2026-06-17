Looking at the exact code paths in question:

The `set_fee_recipient.move` comment is the decisive piece of evidence. Line 1 explicitly states: *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore. This module is kept for backward compatibility."*

Combined with the full code trace, this is a confirmed vulnerability.

---

### Title
Fees Paid to `update_single_price_feed` Are Permanently Locked in `PriceInfoObject` With No On-Chain Withdrawal Path — (`target_chains/sui/contracts/sources/price_info.move`)

### Summary
Every call to `update_single_price_feed` unconditionally deposits the caller's SUI fee coin into the target `PriceInfoObject` before the freshness check. Because `price_info.move` exposes no withdrawal function and the `fee_recipient_address` mechanism is explicitly abandoned (per the comment in `set_fee_recipient.move`), all collected fees are permanently frozen in shared objects with no recovery path.

### Finding Description

**Step 1 — Fee deposited before freshness check.**

In `pyth::pyth::update_single_price_feed`, the fee is deposited unconditionally on line 277, before `update_cache` is called on line 287:

```move
// store fee coins within price info object
price_info::deposit_fee_coins(price_info_object, fee);   // line 277 — always runs

// ...
update_cache(latest_only, cur_price_info, price_info_object, clock);  // line 287
``` [1](#0-0) 

**Step 2 — Freshness check is inside `update_cache`, after the deposit.**

`update_cache` only updates the price if `is_fresh_update` returns true. A stale update (`update_timestamp <= cached_timestamp`) silently skips the price write, but the fee has already been deposited. [2](#0-1) 

**Step 3 — `deposit_fee_coins` has no inverse.**

`price_info.move` provides `deposit_fee_coins` (public) and `get_balance` (read-only), but zero withdrawal or transfer functions. The fee is stored as a dynamic object field on the `PriceInfoObject` with key `b"fee_storage"` and can never leave. [3](#0-2) 

**Step 4 — `fee_recipient_address` is explicitly dead code.**

`set_fee_recipient.move` line 1 states verbatim: *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore. This module is kept for backward compatibility."* The field exists in `State` and has a getter/setter, but is never referenced in any transfer call anywhere in the contract suite. [4](#0-3) [5](#0-4) 

### Impact Explanation
Every SUI fee ever paid via `update_single_price_feed` — whether for a fresh or stale update — is permanently locked inside the corresponding `PriceInfoObject`. The protocol cannot collect revenue, and users cannot recover overpaid or wasted fees. For stale updates specifically, the caller pays the full fee and receives zero benefit (no price update). Over time, across all price feeds and all callers, this represents an unbounded accumulation of permanently frozen protocol funds.

### Likelihood Explanation
This is triggered by every normal use of the protocol. Any caller of `update_single_price_feed` — including legitimate integrators — contributes to the locked balance. An adversary wishing to maximize locked funds need only submit repeated calls with a stale price and a valid fee coin; no privileged access is required.

### Recommendation
1. Add a `withdraw_fee_coins` function (gated to `public(friend)` or a governance capability) to `price_info.move` that transfers the accumulated `Coin<SUI>` from the `FEE_STORAGE_KEY` dynamic field to `fee_recipient_address`.
2. Move the `deposit_fee_coins` call to **after** the `is_fresh_update` check inside `update_cache`, so stale updates revert without consuming the fee (or return the coin to the caller).
3. Remove or repurpose the vestigial `fee_recipient_address` / `set_fee_recipient` governance action to actually drive fee withdrawals.

### Proof of Concept
```move
// 1. Call update_single_price_feed with a stale price (update_timestamp <= cached_timestamp)
//    and a valid fee coin of value >= base_update_fee.
// 2. The call succeeds (does not abort).
// 3. Assert price_info::get_balance(&price_info_object) == base_update_fee  // fee deposited
// 4. Assert the price inside price_info_object is UNCHANGED               // no update applied
// 5. Observe that no function in price_info.move can move those coins out.
// 6. Repeat N times: balance grows to N * base_update_fee, permanently locked.
```

The existing test `test_create_and_update_price_feeds_with_batch_attestation_success` already asserts `price_info::get_balance(&price_info_object_1) == DEFAULT_BASE_UPDATE_FEE` after a single update, confirming the deposit path. [6](#0-5)   No corresponding withdrawal assertion or function exists anywhere in the test suite or production code.

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L274-288)
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
                break
```

**File:** target_chains/sui/contracts/sources/pyth.move (L316-322)
```text
        if (is_fresh_update(update, price_info_object)){
            pyth_event::emit_price_feed_update(price_feed::from(price_info::get_price_feed(update)), clock::timestamp_ms(clock)/1000);
            price_info::update_price_info_object(
                price_info_object,
                update
            );
        }
```

**File:** target_chains/sui/contracts/sources/pyth.move (L817-817)
```text
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

**File:** target_chains/sui/contracts/sources/state.move (L48-48)
```text
        fee_recipient_address: address,
```
