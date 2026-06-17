### Title
Permanent Locking of SUI Fee Revenue in `PriceInfoObject` Dynamic Fields — No Withdrawal Mechanism Exists (`target_chains/sui/contracts/sources/price_info.move`)

---

### Summary

Every call to `update_single_price_feed` deposits SUI fee coins into a `PriceInfoObject` dynamic object field. No function in the entire Sui contract suite extracts those coins. The `fee_recipient_address` stored in `State` is explicitly documented as vestigial and is never consulted during fee routing. All collected fees are permanently locked.

---

### Finding Description

`update_single_price_feed` in `pyth.move` unconditionally calls `price_info::deposit_fee_coins` before performing any price update: [1](#0-0) 

`deposit_fee_coins` either creates a new `Coin<SUI>` dynamic object field under `FEE_STORAGE_KEY` or joins the incoming coins into the existing one: [2](#0-1) 

Searching the entire `price_info.move` file reveals only two fee-related functions: `deposit_fee_coins` (write) and `get_balance` (read). There is no `withdraw_fee_coins`, no `transfer_fees`, and no function that removes the `Coin<SUI>` dynamic object field from a `PriceInfoObject`: [3](#0-2) 

The `State` struct stores a `fee_recipient_address` field, and a governance action (`set_fee_recipient`) can update it. However, the module's own comment makes the situation explicit:

> *"The previous version of the contract sent the fees to a recipient address but this state is not used anymore"* [4](#0-3) 

`state::get_fee_recipient` is a public getter, but no production code path ever reads it to route fees: [5](#0-4) 

---

### Impact Explanation

Every SUI fee paid by any updater — at any `base_update_fee > 0` — is permanently locked inside the corresponding `PriceInfoObject` shared object. There is no on-chain path for the protocol operator, the fee recipient, or any governance action to recover those coins. The `set_fee_recipient` governance action updates a field that is never read during fee routing, so executing it has no effect on the locked balance. Over time, the total locked value equals `N × base_update_fee` across all updates, with no upper bound.

---

### Likelihood Explanation

This triggers on every legitimate price update when `base_update_fee > 0`. No special attacker role, leaked key, or malicious governance majority is required. The path is: deploy with non-zero fee → any user calls `update_single_price_feed` → fee is locked. It is unconditional and continuous.

---

### Recommendation

Add a `withdraw_fee_coins` function (gated to `friend pyth::pyth` or a governance capability) that removes the `Coin<SUI>` dynamic object field from a `PriceInfoObject` and transfers it to `state::get_fee_recipient(pyth_state)`. Alternatively, route fees directly to the recipient address inside `update_single_price_feed` instead of depositing them into the `PriceInfoObject`. The vestigial `fee_recipient_address` field and `set_fee_recipient` governance action already provide the necessary plumbing — the missing piece is the actual transfer call.

---

### Proof of Concept

```
// Invariant fuzz test (pseudocode)
let fee = base_update_fee; // non-zero
for i in 0..N {
    update_single_price_feed(pyth_state, price_updates, &mut pio, coin::mint(fee), clock);
}
assert!(price_info::get_balance(&pio) == N * fee);  // always true
// grep price_info.move for "withdraw" → 0 results
// grep pyth.move for "transfer" after deposit_fee_coins → 0 results
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

**File:** target_chains/sui/contracts/sources/price_info.move (L1-116)
```text
module pyth::price_info {
    use sui::object::{Self, UID, ID};
    use sui::tx_context::{TxContext};
    use sui::dynamic_object_field::{Self};
    use sui::table::{Self};
    use sui::coin::{Self, Coin};
    use sui::sui::SUI;

    use pyth::price_feed::{Self, PriceFeed};
    use pyth::price_identifier::{PriceIdentifier};

    const KEY: vector<u8> = b"price_info";
    const FEE_STORAGE_KEY: vector<u8> = b"fee_storage";
    const E_PRICE_INFO_REGISTRY_ALREADY_EXISTS: u64 = 0;
    const E_PRICE_IDENTIFIER_ALREADY_REGISTERED: u64 = 1;
    const E_PRICE_IDENTIFIER_NOT_REGISTERED: u64 = 2;

    friend pyth::pyth;
    friend pyth::state;

    /// Sui object version of PriceInfo.
    /// Has a key ability, is unique for each price identifier, and lives in global store.
    struct PriceInfoObject has key, store {
        id: UID,
        price_info: PriceInfo
    }

    /// Copyable and droppable.
    struct PriceInfo has copy, drop, store {
        attestation_time: u64,
        arrival_time: u64,
        price_feed: PriceFeed,
    }

    /// Creates a table which maps a PriceIdentifier to the
    /// UID (in bytes) of the corresponding Sui PriceInfoObject.
    public(friend) fun new_price_info_registry(parent_id: &mut UID, ctx: &mut TxContext) {
        assert!(
            !dynamic_object_field::exists_(parent_id, KEY),
            E_PRICE_INFO_REGISTRY_ALREADY_EXISTS
        );
        dynamic_object_field::add(
            parent_id,
            KEY,
            table::new<PriceIdentifier, ID>(ctx)
        )
    }

    public(friend) fun add(parent_id: &mut UID, price_identifier: PriceIdentifier, id: ID) {
        assert!(
            !contains(parent_id, price_identifier),
            E_PRICE_IDENTIFIER_ALREADY_REGISTERED
        );
        table::add(
            dynamic_object_field::borrow_mut(parent_id, KEY),
            price_identifier,
            id
        )
    }


    /// Returns ID of price info object corresponding to price_identifier as a byte vector.
    public fun get_id_bytes(parent_id: &UID, price_identifier: PriceIdentifier): vector<u8> {
        assert!(
            contains(parent_id, price_identifier),
            E_PRICE_IDENTIFIER_NOT_REGISTERED
        );
        object::id_to_bytes(
            table::borrow<PriceIdentifier, ID>(
                dynamic_object_field::borrow(parent_id, KEY),
                price_identifier
            )
        )
    }

    /// Returns ID of price info object corresponding to price_identifier as an ID.
    public fun get_id(parent_id: &UID, price_identifier: PriceIdentifier): ID {
        assert!(
            contains(parent_id, price_identifier),
            E_PRICE_IDENTIFIER_NOT_REGISTERED
        );
        object::id_from_bytes(
            object::id_to_bytes(
                table::borrow<PriceIdentifier, ID>(
                    dynamic_object_field::borrow(parent_id, KEY),
                    price_identifier
                )
            )
        )
    }

    public fun contains(parent_id: &UID, price_identifier: PriceIdentifier): bool {
        let ref = dynamic_object_field::borrow(parent_id, KEY);
        table::contains<PriceIdentifier, ID>(ref, price_identifier)
    }

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

**File:** target_chains/sui/contracts/sources/governance/set_fee_recipient.move (L1-18)
```text
/// The previous version of the contract sent the fees to a recipient address but this state is not used anymore
/// This module is kept for backward compatibility
module pyth::set_fee_recipient {
    use wormhole::cursor;
    use wormhole::external_address::{Self};

    use pyth::state::{Self, State, LatestOnly};

    friend pyth::governance;

    struct PythFeeRecipient {
        recipient: address
    }

    public(friend) fun execute(latest_only: &LatestOnly, state: &mut State, payload: vector<u8>) {
        let PythFeeRecipient { recipient } = from_byte_vec(payload);
        state::set_fee_recipient(latest_only, state, recipient);
    }
```

**File:** target_chains/sui/contracts/sources/state.move (L119-121)
```text
    public fun get_fee_recipient(s: &State): address {
        s.fee_recipient_address
    }
```
