### Title
Missing Protection Against Pyth Fee Increase Permanently Locks User Funds in Echo.executeCallback — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` fetches the Pyth update fee **dynamically at fulfillment time**, but the user locked in a fee computed against `_state.pythFeeInWei` **at request time**. If the Pyth fee rises between request and fulfillment, the fee-accounting arithmetic underflows and reverts, making the request permanently unfulfillable. No cancellation or refund path exists, so the user's ETH is locked forever.

---

### Finding Description

**Request phase** — `requestPriceUpdatesWithCallback` (Echo.sol line 75–84):

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
// ...
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);   // provider portion
_state.accruedFeesInWei += _state.pythFeeInWei;                 // pyth portion
```

The user pays exactly `_state.pythFeeInWei` toward the Pyth contract and the remainder goes to the provider. The Pyth portion is **consumed immediately** by `accruedFeesInWei`; it is not held in escrow for the later `parsePriceFeedUpdates` call.

**Fulfillment phase** — `executeCallback` (Echo.sol line 145–162):

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);          // live fee at fulfillment time
PythStructs.PriceFeed[] memory priceFeeds =
    pyth.parsePriceFeedUpdates{value: pythFee}(...);

_state.providers[providerToCredit].accruedFeesInWei +=
    SafeCast.toUint128((req.fee + msg.value) - pythFee);  // underflows if pythFee > req.fee + msg.value
```

`pythFee` is the **current** Pyth update fee, which can differ from `_state.pythFeeInWei` that was in effect at request time. If the Pyth fee has increased and the caller of `executeCallback` sends no additional ETH (`msg.value = 0`), the subtraction `(req.fee + 0) - pythFee` underflows and the entire transaction reverts.

There is **no `cancelRequest`, no refund function, and no expiry sweep** in the Echo contract. The only code path that clears a request is `clearRequest(sequenceNumber)` inside `executeCallback` itself (line 164), which is never reached when the function reverts. The user's ETH is permanently locked.

---

### Impact Explanation

**Impact: High.**  
User ETH paid at request time is irrecoverably locked in the contract. The requester cannot cancel, cannot reclaim funds, and cannot force fulfillment because any `executeCallback` call will revert as long as the live Pyth fee exceeds the stored `req.fee`. The locked amount equals the full `msg.value` paid at request time (provider fee + Pyth fee portion).

---

### Likelihood Explanation

**Likelihood: Low.**  
The Pyth update fee (`_state.pythFeeInWei` in Echo, and the analogous fee in the Pyth core contract) is a governance-controlled parameter that changes infrequently. However, the window between `requestPriceUpdatesWithCallback` and `executeCallback` can be non-trivial (the exclusivity period alone is configurable), and any governance fee increase during that window triggers the issue. No attacker action is required — a routine governance fee update is sufficient.

---

### Recommendation

1. **Escrow the Pyth fee at request time** and use only the escrowed amount in `executeCallback`, ignoring any live fee increase.  
2. **Or** add a `cancelRequest(uint64 sequenceNumber)` function that refunds `msg.value` to `req.requester` when a request has been pending beyond a configurable timeout.  
3. **Or** cap the Pyth fee used in `executeCallback` to `_state.pythFeeInWei` (the value in effect at request time) and require the provider to cover any shortfall from their accrued balance.

---

### Proof of Concept

1. Pyth fee is `100 wei`. User calls `requestPriceUpdatesWithCallback` paying `100 (pythFee) + 500 (providerFee) = 600 wei`.  
   - `req.fee = 600 - 100 = 500`  
   - `_state.accruedFeesInWei += 100`  

2. Pyth governance raises the update fee to `600 wei`.  

3. Provider calls `executeCallback` with `msg.value = 0`.  
   - `pythFee = pyth.getUpdateFee(updateData)` → `600`  
   - `(req.fee + msg.value) - pythFee` = `(500 + 0) - 600` → **underflow → revert**  

4. No other code path can clear the request. The user's `600 wei` is permanently locked. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L323-332)
```text
    function clearRequest(uint64 sequenceNumber) internal {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        Request storage req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            req.sequenceNumber = 0;
        } else {
            delete _state.requestsOverflow[key];
        }
    }
```
