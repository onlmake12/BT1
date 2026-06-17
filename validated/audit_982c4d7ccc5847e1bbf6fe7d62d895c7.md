### Title
User-Controlled `callbackGasLimit` Allows Underpayment of Provider Gas Fees — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

In `Echo.sol`, the `requestPriceUpdatesWithCallback` function accepts a user-supplied `callbackGasLimit` that directly scales the gas-based fee component. A user can set `callbackGasLimit = 0` (or any arbitrarily small value) to pay zero (or near-zero) gas fees to the provider, while the provider still must execute `executeCallback` and pay real gas costs for price feed parsing and callback dispatch.

### Finding Description

`getFee` computes the total fee as:

```solidity
uint256 gasFee = callbackGasLimit * providerFeeInWei;
feeAmount = baseFee + providerBaseFee + providerFeedFee + SafeCast.toUint96(gasFee);
``` [1](#0-0) 

The `callbackGasLimit` is taken directly from the caller with no minimum enforcement:

```solidity
function requestPriceUpdatesWithCallback(
    address provider,
    uint64 publishTime,
    bytes32[] calldata priceIds,
    uint32 callbackGasLimit          // <-- fully user-controlled
) external payable override returns (uint64 requestSequenceNumber) {
    ...
    uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
    if (msg.value < requiredFee) revert InsufficientFee();
``` [2](#0-1) 

The stored `callbackGasLimit` is later used verbatim when dispatching the callback:

```solidity
try IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(sequenceNumber, priceFeeds)
``` [3](#0-2) 

There is no lower bound on `callbackGasLimit`. Setting it to `0` makes `gasFee = 0`, so the user pays only `baseFee + providerBaseFee + providerFeedFee`. The provider still has to submit `executeCallback`, which parses price feeds via `IPyth.parsePriceFeedUpdates` (a non-trivial gas cost), dispatches the (immediately-failing) callback, and emits events — all at the provider's own expense. [4](#0-3) 

By contrast, Entropy's `getProviderFee` enforces a minimum fee equal to `provider.feeInWei` regardless of the gas limit passed:

```solidity
} else {
    return provider.feeInWei;   // minimum always applies
}
``` [5](#0-4) 

Echo has no equivalent floor.

### Impact Explanation

A user can pay only the flat base fees while forcing the provider to execute a full `executeCallback` transaction (including `parsePriceFeedUpdates` and the failed callback dispatch) at the provider's own gas cost. This directly reduces provider revenue and can make operating an Echo provider economically unviable, since every request with `callbackGasLimit = 0` results in a net loss for the provider.

### Likelihood Explanation

The attack requires only a single unprivileged `requestPriceUpdatesWithCallback` call with `callbackGasLimit = 0`. No special role, key, or collusion is needed. Any user who wants price data without paying gas fees can exploit this trivially and repeatedly.

### Recommendation

Enforce a minimum `callbackGasLimit` in `requestPriceUpdatesWithCallback`, either:
- Requiring `callbackGasLimit >= provider.minCallbackGasLimit` (a provider-configured floor), or
- Defaulting to the provider's configured default gas limit when `callbackGasLimit` is below it (mirroring Entropy's behavior in `getProviderFee`).

### Proof of Concept

1. Provider registers with `feePerGasInWei = 1000 wei`, `baseFeeInWei = 0.001 ETH`.
2. Attacker calls `requestPriceUpdatesWithCallback(provider, publishTime, priceIds, 0)` paying only `pythFeeInWei + baseFeeInWei + feePerFeedInWei * priceIds.length` (zero gas fee).
3. Provider's keeper sees the request and calls `executeCallback`, spending ~200k+ gas on `parsePriceFeedUpdates` and event emission.
4. The `_echoCallback{gas: 0}` call immediately fails; `PriceUpdateCallbackFailed` is emitted.
5. Provider accrues `req.fee + msg.value - pythFee` — which equals only the flat fees, not the gas cost of executing the callback.
6. Repeating this at scale drains the provider's operational budget with no recourse.

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L144-162)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L177-179)
```text
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L249-254)
```text
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L791-793)
```text
        } else {
            return provider.feeInWei;
        }
```
