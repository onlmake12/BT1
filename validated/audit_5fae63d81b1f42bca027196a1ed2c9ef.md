### Title
Pyth Oracle Fee Fluctuation Between Request and Execution Can Lock User Funds in Echo Contract - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

The `Echo` contract implements a two-step price-update-with-callback flow. The user pays a fee at request time, but the actual Pyth oracle fee is computed dynamically at execution time. If the Pyth oracle's `singleUpdateFeeInWei` increases between the two steps, `executeCallback` can revert due to arithmetic underflow, and since no cancellation mechanism exists, user funds are permanently locked.

### Finding Description

In `requestPriceUpdatesWithCallback`, the user pays `msg.value` and the contract stores:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

`_state.pythFeeInWei` is Echo's own protocol fee (credited to `_state.accruedFeesInWei`). The provider's fees (`baseFeeInWei + feePerFeedInWei + feePerGasInWei`) are supposed to cover the Pyth oracle's `getUpdateFee()` cost at execution time, but this oracle fee is **not locked in at request time**.

In `executeCallback`, the actual Pyth oracle fee is computed live:

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{value: pythFee}(...);

_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);   // ← underflows if pythFee > req.fee + msg.value
```

The Pyth oracle's `singleUpdateFeeInWei` is mutable via governance (`setSingleUpdateFeeInWei`). If it increases between the two steps:

1. `pythFee` at execution time exceeds `req.fee` (the amount the user deposited minus Echo's protocol cut).
2. The subtraction `(req.fee + msg.value) - pythFee` underflows and reverts (Solidity 0.8 checked arithmetic).
3. `clearRequest` is never reached, so the request stays active but unfulfillable at the new fee level.
4. There is **no `cancelRequest` function** in the Echo contract — the user cannot recover their locked ETH.

The provider could theoretically add extra `msg.value` to `executeCallback` to cover the shortfall, but this means the provider pays out of pocket with no reimbursement path, so rational providers will simply not execute, leaving the request permanently stuck.

### Impact Explanation

**Impact: Medium**

- User ETH locked in the contract with no recovery path if the Pyth oracle fee rises enough to make `pythFee > req.fee`.
- Even a moderate governance-driven fee increase (e.g., 2×) on a request with a tight fee margin is sufficient to trigger the revert.
- Providers are economically disincentivized from executing callbacks that would cost them ETH, compounding the lock.

### Likelihood Explanation

**Likelihood: Medium**

- The Pyth oracle's `singleUpdateFeeInWei` is a governance-controlled parameter that has historically been adjusted.
- The Echo contract is designed for asynchronous fulfillment; requests can remain pending for seconds to minutes, a window during which a governance fee update can land.
- No special attacker capability is required — a routine governance fee increase is sufficient to trigger the condition for any in-flight request.

### Recommendation

1. **Snapshot the Pyth oracle fee at request time** and store it in `req`. At execution time, verify that the actual `pythFee` does not exceed the snapshotted value (or add a tolerance parameter).
2. **Add a `cancelRequest` function** that allows the requester to reclaim their `req.fee` if the request has not been fulfilled within a configurable timeout.
3. Alternatively, **include the Pyth oracle fee in `getFee()`** by calling `pyth.getUpdateFee()` at request time and storing the result, so the user's payment is guaranteed to cover it.

### Proof of Concept

```
1. Provider registers with feePerGasInWei = 1 wei/gas, callbackGasLimit = 100_000.
   Pyth oracle singleUpdateFeeInWei = 1 wei → getUpdateFee(1 feed) = 1 wei.

2. User calls requestPriceUpdatesWithCallback, paying exactly getFee() = 
   pythFeeInWei(10) + providerBaseFee(5) + providerFeedFee(2) + gasFee(100_000) = 100_017 wei.
   req.fee = 100_017 - 10 = 100_007 wei.

3. Pyth governance calls setSingleUpdateFeeInWei(200_000).
   Now getUpdateFee(1 feed) = 200_000 wei.

4. Provider calls executeCallback (msg.value = 0):
   pythFee = 200_000 wei
   (req.fee + msg.value) - pythFee = 100_007 - 200_000 → underflow → REVERT

5. No cancelRequest exists. User's 100_017 wei is permanently locked.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-84)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L12-29)
```text
    struct Request {
        // Slot 1: 8 + 8 + 4 + 12 = 32 bytes
        uint64 sequenceNumber;
        uint64 publishTime;
        uint32 callbackGasLimit;
        uint96 fee;
        // Slot 2: 20 + 12 = 32 bytes
        address requester;
        // 12 bytes padding

        // Slot 3: 20 + 12 = 32 bytes
        address provider;
        // 12 bytes padding

        // Dynamic array starts at its own slot
        // Store only first 8 bytes of each price ID to save gas
        bytes8[] priceIdPrefixes;
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L54-75)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable returns (uint64 sequenceNumber);

    /**
     * @notice Executes the callback for a price update request
     * @dev Requires 1.5x the callback gas limit to account for cross-contract call overhead
     * For example, if callbackGasLimit is 1M, the transaction needs at least 1.5M gas + some gas for some other operations in the function before the callback
     * @param providerToCredit The provider to credit for fulfilling the request. This may not be the provider that submitted the request (if the exclusivity period has elapsed).
     * @param sequenceNumber The sequence number of the request
     * @param updateData The raw price update data from Pyth
     * @param priceIds The price feed IDs to update, must match the request
     */
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable;
```
