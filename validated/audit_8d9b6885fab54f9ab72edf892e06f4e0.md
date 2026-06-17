### Title
Unsigned Integer Underflow in `executeCallback` Blocks Callback Fulfillment and Locks User Funds - (`Echo.sol`)

### Summary

In `Echo.sol`, the `executeCallback` function computes the provider's credit as `(req.fee + msg.value) - pythFee`, where `pythFee` is the **actual** Pyth fee at execution time. Because `req.fee` was fixed at request time using the then-current `_state.pythFeeInWei`, any increase in the Pyth fee between request and execution can cause `pythFee > req.fee + msg.value`, triggering an unsigned integer underflow revert. This permanently blocks callback fulfillment and locks the user's funds in the contract.

### Finding Description

At request time, `Echo.sol` stores the provider's share of the user's payment:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [1](#0-0) 

At execution time, `executeCallback` fetches the **live** Pyth fee and pays it:

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(...);
``` [2](#0-1) 

Then it credits the provider:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [3](#0-2) 

The underflow window exists because the contract holds `req.fee + _state.pythFeeInWei + msg.value` ETH (user's original payment plus executor's payment), so `parsePriceFeedUpdates` succeeds whenever `pythFee ≤ req.fee + _state.pythFeeInWei + msg.value`. However, the subtraction `(req.fee + msg.value) - pythFee` underflows whenever `pythFee > req.fee + msg.value`. Both conditions hold simultaneously when:

```
req.fee + msg.value  <  pythFee  ≤  req.fee + _state.pythFeeInWei + msg.value
```

That is, when the live Pyth fee has grown by any amount between 1 wei and `_state.pythFeeInWei` above the provider-fee portion of the user's payment.

Critically, `clearRequest` is called **after** the underflow line:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);  // reverts here

clearRequest(sequenceNumber);  // never reached
``` [4](#0-3) 

Because the entire transaction reverts, the request is never cleared and the user's funds remain locked.

### Impact Explanation

A user who submitted a price-update request via `requestPriceUpdatesWithCallback` cannot have their callback fulfilled. Their ETH is permanently locked in the contract unless they (or the executor) call `executeCallback` again with enough additional `msg.value` to satisfy `req.fee + msg.value ≥ pythFee`. There is no dedicated recovery path for this scenario, and the user bears an unexpected additional cost they did not agree to at request time.

### Likelihood Explanation

The Pyth contract's `getUpdateFee` is dynamic and can increase due to governance changes or changes in the number of price IDs in the update data. The `_state.pythFeeInWei` in Echo is a fixed value set at initialization and is not automatically synchronized with the live Pyth fee. Any period where the live Pyth fee exceeds the stored `_state.pythFeeInWei` by even 1 wei triggers this condition for requests made when the fee was lower. This is a normal operational scenario requiring no privileged access or malicious action.

### Recommendation

Replace the unchecked subtraction with a safe version that accounts for the case where `pythFee` exceeds the available provider share. One approach:

```solidity
uint128 available = SafeCast.toUint128(req.fee + msg.value);
uint128 providerCredit = pythFee <= available
    ? available - SafeCast.toUint128(pythFee)
    : 0;
_state.providers[providerToCredit].accruedFeesInWei += providerCredit;
```

Additionally, `_state.pythFeeInWei` should be kept in sync with the live Pyth fee (or the fee check at request time should use `pyth.getUpdateFee` directly) to prevent systematic under-collection.

### Proof of Concept

1. Admin deploys `EchoUpgradeable` with `pythFeeInWei = 100 wei`.
2. Provider registers with `baseFeeInWei = 50 wei`, `feePerFeedInWei = 0`, `feePerGasInWei = 0`.
3. User calls `requestPriceUpdatesWithCallback{value: 150 wei}(provider, publishTime, priceIds, gasLimit)`.
   - `req.fee = 150 - 100 = 50 wei`
   - `_state.accruedFeesInWei += 100`
4. Pyth governance increases the Pyth update fee to 120 wei.
5. Executor calls `executeCallback{value: 0}(provider, seqNum, updateData, priceIds)`.
   - `pythFee = pyth.getUpdateFee(updateData) = 120 wei`
   - Contract balance = 150 wei ≥ 120 wei → `parsePriceFeedUpdates{value: 120}` **succeeds**
   - `(req.fee + msg.value) - pythFee = (50 + 0) - 120` → **unsigned integer underflow → revert**
6. The request is never cleared. The user's 150 wei is permanently locked. The callback is never delivered. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L84-84)
```text
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-165)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }

        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

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
