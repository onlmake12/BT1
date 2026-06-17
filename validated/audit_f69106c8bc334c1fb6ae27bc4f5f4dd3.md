### Title
Flat Pyth Protocol Fee Per Request in Echo Regardless of Price Feed Count — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

The `Echo` contract charges a flat `pythFeeInWei` to the Pyth protocol per `requestPriceUpdatesWithCallback` call, regardless of how many price feeds (`priceIds`) are included in the request. A user requesting the maximum of 10 price feeds pays the same Pyth protocol fee as a user requesting 1 price feed, directly analogous to the sheepDog per-user vs. per-sheep fee bug.

---

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback` accepts up to `MAX_PRICE_IDS = 10` price feed IDs per request. The total fee is computed by `getFee`:

```solidity
uint96 baseFee = _state.pythFeeInWei; // Fixed fee to Pyth
uint96 providerFeedFee = SafeCast.toUint96(
    priceIds.length * _state.providers[provider].feePerFeedInWei
);
``` [1](#0-0) 

The provider's portion scales with `priceIds.length` via `feePerFeedInWei`, but the Pyth protocol portion (`pythFeeInWei`) is a **flat fee per request**. When the request is stored, only the flat `pythFeeInWei` is credited to the protocol:

```solidity
_state.accruedFeesInWei += _state.pythFeeInWei;
``` [2](#0-1) 

This is structurally identical to the sheepDog bug: the fee is charged per request (per user) rather than per price feed (per sheep).

---

### Impact Explanation

The Pyth protocol accrues `pythFeeInWei` regardless of whether 1 or 10 price feeds are requested. A user who needs 10 price feeds can batch them into a single Echo request and pay the same Pyth protocol fee as a user requesting 1 feed. This reduces Pyth protocol revenue by up to 10× for maximally batched requests. The `accruedFeesInWei` balance — which is withdrawn by the admin — is systematically undercollected relative to the number of price feeds served. [3](#0-2) 

---

### Likelihood Explanation

Any user or protocol integrating Echo who needs multiple price feeds will naturally batch them into a single request (it is cheaper and simpler). This is the default usage pattern. The `MAX_PRICE_IDS = 10` cap means the maximum fee discount is 10×. No special access or privilege is required — any `msg.sender` can call `requestPriceUpdatesWithCallback` with up to 10 price IDs. [4](#0-3) 

---

### Recommendation

Scale the Pyth protocol fee by the number of price feeds requested, consistent with how the underlying `IPyth` contract charges fees (per price message). Replace the flat accrual with:

```solidity
_state.accruedFeesInWei += _state.pythFeeInWei * priceIds.length;
```

and update `getFee` accordingly:

```solidity
uint96 baseFee = SafeCast.toUint96(_state.pythFeeInWei * priceIds.length);
``` [5](#0-4) 

---

### Proof of Concept

1. Deploy Echo with `pythFeeInWei = 1000 wei`.
2. **User A** calls `requestPriceUpdatesWithCallback(provider, t, [feedId1], gasLimit)` with `msg.value = getFee(...)`. Pyth accrues **1000 wei**.
3. **User B** calls `requestPriceUpdatesWithCallback(provider, t, [feedId1, feedId2, ..., feedId10], gasLimit)` with `msg.value = getFee(...)`. Pyth accrues **1000 wei** — the same amount despite serving 10× the price feeds.
4. Confirm: `getAccruedPythFees()` returns `2000 wei` after both calls, even though 11 total price feed slots were served. A correctly scaled fee would return `11000 wei`. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-76)
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-99)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L235-255)
```text
    function getFee(
        address provider,
        uint32 callbackGasLimit,
        bytes32[] calldata priceIds
    ) public view override returns (uint96 feeAmount) {
        uint96 baseFee = _state.pythFeeInWei; // Fixed fee to Pyth
        // Note: The provider needs to set its fees to include the fee charged by the Pyth contract.
        // Ideally, we would be able to automatically compute the pyth fees from the priceIds, but the
        // fee computation on IPyth assumes it has the full updated data.
        uint96 providerBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 providerFeedFee = SafeCast.toUint96(
            priceIds.length * _state.providers[provider].feePerFeedInWei
        );
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L8-10)
```text
    // Maximum number of price feeds per request. This limit keeps gas costs predictable and reasonable. 10 is a reasonable number for most use cases.
    // Requests with more than 10 price feeds should be split into multiple requests
    uint8 public constant MAX_PRICE_IDS = 10;
```
