### Title
Permanent Fund Lock via Unvalidated `priceIds` in `Echo.requestPriceUpdatesWithCallback` — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.requestPriceUpdatesWithCallback` accepts any `priceIds` array without verifying that the IDs correspond to real, fulfillable Pyth price feeds. A request submitted with a non-existent or invalid price feed ID is accepted and fees are collected, but `executeCallback` will always revert when it tries to parse update data for that ID. Because there is no cancellation or refund path, the user's fee payment is permanently locked in the contract.

---

### Finding Description

`requestPriceUpdatesWithCallback` performs the following validations at request time:

- Provider is registered
- `publishTime` is not more than 60 seconds in the future
- `priceIds.length <= MAX_PRICE_IDS`
- `msg.value >= getFee(...)`

It does **not** validate that each element of `priceIds` is a real Pyth price feed ID. It stores only the first 8 bytes of each ID as a prefix:

```solidity
req.priceIdPrefixes = new bytes8[](priceIds.length);
for (uint8 i = 0; i < priceIds.length; i++) {
    bytes32 priceId = priceIds[i];
    bytes8 prefix;
    assembly { prefix := priceId }
    req.priceIdPrefixes[i] = prefix;
}
```

At fulfillment time, `executeCallback` calls `parsePriceFeedUpdates` on the Pyth oracle with the full 32-byte IDs supplied by the executor:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{value: pythFee}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)
);
```

`parsePriceFeedUpdates` reverts if any requested price ID is not present in `updateData`. If the original request contained a non-existent price feed ID, no valid `updateData` can ever be constructed for it, so `executeCallback` will always revert. The fee accounting and `clearRequest` occur **after** this call:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast.toUint128((req.fee + msg.value) - pythFee);
clearRequest(sequenceNumber);
```

Because the revert unwinds the entire transaction, `clearRequest` is never reached. The request remains active forever, and the user's fee (`req.fee = msg.value - pythFeeInWei`) is permanently locked in the contract. There is no `cancelRequest`, `refundRequest`, or timeout-based recovery function anywhere in `Echo.sol`.

---

### Impact Explanation

Any user who submits a `requestPriceUpdatesWithCallback` with a `priceId` that does not correspond to a real Pyth price feed (whether by mistake or by being tricked through a malicious wrapper contract) will have their entire fee payment permanently frozen in the Echo contract. The funds cannot be recovered by the user, the provider, or the admin, because no refund path exists.

**Severity**: High — permanent, irrecoverable loss of user funds.

---

### Likelihood Explanation

- Any EOA or contract can call `requestPriceUpdatesWithCallback` with arbitrary `priceIds`.
- A malicious contract wrapping Echo could silently substitute a fake price ID, causing the downstream user's funds to be locked.
- A user who mistypes or copy-pastes an incorrect price feed ID (e.g., from a different chain) will lose their fee with no recourse.
- No privileged access is required; the entry path is fully permissionless.

---

### Recommendation

1. **Validate price IDs at request time**: Maintain an on-chain registry of accepted price feed IDs, or call `getPrice`/`priceFeedExists` on the Pyth contract to confirm each ID is known before accepting the request.
2. **Add a cancellation / timeout refund path**: Allow the requester to reclaim their fee if the request has not been fulfilled within a configurable timeout window. This is a defense-in-depth measure that also mitigates provider non-fulfillment.
3. **Store full 32-byte price IDs**: The current 8-byte prefix storage means the prefix check in `executeCallback` cannot catch collisions in the lower 24 bytes, compounding the validation gap.

---

### Proof of Concept

1. Alice calls `requestPriceUpdatesWithCallback` with `priceIds = [bytes32(0xdeadbeef...)]` (a fabricated, non-existent Pyth price feed ID) and pays the required fee.
2. The call succeeds: a sequence number is assigned, `req.fee` is set to `msg.value - pythFeeInWei`, and `_state.accruedFeesInWei` is incremented.
3. The provider attempts `executeCallback` with any `updateData`. `pyth.parsePriceFeedUpdates` reverts because `0xdeadbeef...` is not in the update data.
4. `executeCallback` reverts. `clearRequest` is never called. Alice's fee remains in the contract.
5. No function in `Echo.sol` allows Alice, the provider, or the admin to recover the locked fee.

Relevant code: [1](#0-0) [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-102)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable override returns (uint64 requestSequenceNumber) {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );

        // FIXME: this comment is wrong. (we're not using tx.gasprice)
        // NOTE: The 60-second future limit on publishTime prevents a DoS vector where
        //      attackers could submit many low-fee requests for far-future updates when gas prices
        //      are low, forcing executors to fulfill them later when gas prices might be much higher.
        //      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
        //      the fee estimation unreliable.
        require(publishTime <= block.timestamp + 60, "Too far in future");
        if (priceIds.length > MAX_PRICE_IDS) {
            revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
        }
        requestSequenceNumber = _state.currentSequenceNumber++;

        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
        _state.accruedFeesInWei += _state.pythFeeInWei;

        emit PriceUpdateRequested(req, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L143-165)
```text
        // TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );

        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);

```
