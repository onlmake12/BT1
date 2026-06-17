### Title
Excess ETH Permanently Lost to Provider on Overpayment in `requestPriceUpdatesWithCallback` - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.sol`'s `requestPriceUpdatesWithCallback` accepts `msg.value >= requiredFee` but stores the entire surplus (above `pythFeeInWei`) into `req.fee`, which is later fully credited to the fulfilling provider in `executeCallback`. A user who accidentally overpays has no refund path; the excess is permanently transferred to the provider.

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback` enforces only a lower-bound fee check:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
...
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
_state.accruedFeesInWei += _state.pythFeeInWei;
``` [1](#0-0) 

If `msg.value = requiredFee + X` (where X > 0), then `req.fee` absorbs the full surplus `X` on top of the legitimate provider fee. When `executeCallback` is later called, the entire `req.fee` (including the surplus) is credited to the provider:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [2](#0-1) 

There is no refund path for the requester. The `IEcho` interface NatSpec explicitly states the intent is strict equality — `"The msg.value must be equal to getFee(callbackGasLimit)"` — but the implementation only enforces `>=`. [3](#0-2) 

### Impact Explanation

Any user (EOA or contract) who sends more ETH than `getFee(provider, callbackGasLimit, priceIds)` permanently loses the surplus to the provider. There is no withdrawal function, no refund hook, and no admin recovery path for the requester's excess. The provider receives an unearned windfall equal to the overpayment.

### Likelihood Explanation

Medium. Smart-contract integrators commonly add a small buffer to fee estimates to guard against fee increases between the `getFee` call and the transaction landing. Any such buffer is silently consumed by the provider. Additionally, if a provider raises their fee between the `getFee` call and the transaction, the user may have pre-funded with a higher amount, sending the difference to the provider.

### Recommendation

Replace the lower-bound check with strict equality, matching the documented intent:

```solidity
if (msg.value != requiredFee) revert InvalidFee();
```

Alternatively, refund any surplus after the fee accounting:

```solidity
if (msg.value > requiredFee) {
    (bool ok, ) = payable(msg.sender).call{value: msg.value - requiredFee}("");
    require(ok, "refund failed");
}
```

### Proof of Concept

1. Provider registers with `baseFeeInWei = 1000`, `feePerFeedInWei = 100`, `feePerGasInWei = 1`. `pythFeeInWei = 500`.
2. User calls `getFee(provider, 100000, [feedId])` → returns `500 + 1000 + 100 + 100000 = 101600 wei`.
3. User sends `msg.value = 102000 wei` (400 wei buffer).
4. `req.fee = 102000 - 500 = 101500` (correct fee is `101100`; surplus 400 absorbed).
5. Provider calls `executeCallback`; `pythFee = pyth.getUpdateFee(updateData) = 1 wei`.
6. Provider accrues `101500 + 0 - 1 = 101499 wei` instead of the correct `101099 wei`.
7. User's 400 wei surplus is permanently transferred to the provider with no recourse. [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L41-42)
```text
     * @dev The msg.value must be equal to getFee(callbackGasLimit)
     * @param provider The provider to fulfill the request
```
