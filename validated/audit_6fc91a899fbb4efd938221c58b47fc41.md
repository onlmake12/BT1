The code confirms this is a real vulnerability. Here is the full analysis:

---

### Title
Fee Recipient Dead Code — All Update Fees Permanently Locked in `PriceInfoObject` Dynamic Fields — (`target_chains/sui/contracts/sources/price_info.move`, `pyth.move`, `state.move`)

### Summary
Every SUI fee paid through `update_single_price_feed` is deposited into a `PriceInfoObject` dynamic field via `price_info::deposit_fee_coins`. No function in the entire module set can remove those coins. The `fee_recipient_address` stored in `State` is never used to receive funds; the governance module that manages it even self-documents that the fee-recipient mechanism "is not used anymore." All collected fees are irrecoverably locked.

### Finding Description

`update_single_price_feed` collects a fee from the caller and immediately stores it inside the target `PriceInfoObject`: [1](#0-0) 

`deposit_fee_coins` is declared `public` and only ever accumulates coins — it merges the incoming `Coin<SUI>` into a dynamic object field keyed by `FEE_STORAGE_KEY`: [2](#0-1) 

There is no corresponding `withdraw_fee_coins`, no `public(friend)` drain function, and no governance action that touches the balance stored inside `PriceInfoObject`. A full-text search for `withdraw`, `transfer.*fee`, `coin::split`, and `balance.*extract` across all production `.move` files returns zero hits in the fee-handling path.

`State` stores a `fee_recipient_address` field: [3](#0-2) 

but that address is never the destination of a `sui::transfer` or `coin::split` call anywhere in the codebase. The governance module that manages this field explicitly acknowledges the dead code: [4](#0-3) 

`set_fee_recipient` only updates the stored address; it never moves coins: [5](#0-4) 

### Impact Explanation
Every SUI coin paid as an update fee is deposited into a shared `PriceInfoObject` and can never be recovered. The `fee_recipient_address` invariant — that collected fees reach the designated recipient — is permanently broken. The locked funds grow monotonically with every call to `update_single_price_feed` across all price feeds. There is no upgrade path that can retroactively drain the dynamic fields without a new package version that adds a withdrawal function, and even then the coins already locked before the upgrade remain inaccessible until that function is deployed and called.

### Likelihood Explanation
This is triggered by every normal, unprivileged price-update call. No special precondition, no key compromise, no governance majority is required. The locking is unconditional and begins at contract deployment.

### Recommendation
1. Add a `public(friend) fun withdraw_fee_coins(price_info_object: &mut PriceInfoObject, ctx: &mut TxContext): Coin<SUI>` (or equivalent) to `price_info.move`.
2. In `update_single_price_feed`, after collecting the fee, transfer it directly to `state::get_fee_recipient(pyth_state)` using `sui::transfer::public_transfer` instead of calling `deposit_fee_coins`.
3. Remove or repurpose the now-dead `fee_recipient_address` field in `State`, or wire it to an actual transfer so the governance `set_fee_recipient` action has observable effect.

### Proof of Concept
```
// 1. Deploy contract; fee_recipient = ADMIN
// 2. Unprivileged user calls update_single_price_feed N times, each with fee = base_update_fee
// 3. Assert: balance(ADMIN) unchanged
// 4. Assert: price_info::get_balance(price_info_object) == N * base_update_fee
// 5. Assert: no callable function in the module set reduces get_balance to 0
// => All N * base_update_fee MIST are permanently locked.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/sui/contracts/sources/pyth.move (L262-296)
```text
    public fun update_single_price_feed(
        pyth_state: &PythState,
        price_updates: HotPotatoVector<PriceInfo>,
        price_info_object: &mut PriceInfoObject,
        fee: Coin<SUI>,
        clock: &Clock
    ): HotPotatoVector<PriceInfo> {
        let latest_only = state::assert_latest_only(pyth_state);

        // On Sui, users get to choose which price feeds to update. They specify a single price feed to
        // update at a time. We therefore charge the base fee for each such individual update.
        // This is a departure from Eth, where users don't get to necessarily choose.
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
            };
            i = i + 1;
        };
        if (found==false){
            abort E_PRICE_UPDATE_NOT_FOUND_FOR_PRICE_INFO_OBJECT
        };
        price_updates
    }
```

**File:** target_chains/sui/contracts/sources/price_info.move (L97-116)
```text
    public fun get_balance(price_info_object: &PriceInfoObject): u64 {
        if (!dynamic_object_field::exists_with_type<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY)) {
            return 0
        };
        let fee = dynamic_object_field::borrow<vector<u8>, Coin<SUI>>(&price_info_object.id, FEE_STORAGE_KEY);
        coin::value(fee)
    }

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

**File:** target_chains/sui/contracts/sources/state.move (L48-48)
```text
        fee_recipient_address: address,
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-2)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
```

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L15-18)
```text
    public(friend) fun execute(latest_only: &LatestOnly, state: &mut State, payload: vector<u8>) {
        let PythFeeRecipient { recipient } = from_byte_vec(payload);
        state::set_fee_recipient(latest_only, state, recipient);
    }
```
