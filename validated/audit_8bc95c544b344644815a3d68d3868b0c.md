### Title
Unbounded `while` Loop in `executeCallback` Can Cause DoS and Permanently Lock User Funds - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary
`Echo.sol`'s `executeCallback` contains an unbounded `while` loop that linearly scans forward through all fulfilled sequence numbers to advance `_state.firstUnfulfilledSeq`. An attacker can inflate the gap between `firstUnfulfilledSeq` and `currentSequenceNumber` by creating many requests and fulfilling them out of order, causing the loop to exceed the block gas limit when a victim's earlier request is finally fulfilled. The resulting OOG revert undoes `clearRequest`, permanently trapping the victim's funds.

---

### Finding Description

In `executeCallback`, after clearing the current request, the contract runs:

```solidity
clearRequest(sequenceNumber);   // line 164

while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [1](#0-0) 

Each iteration of the loop calls `findRequest`, which performs **two cold storage reads**: one into the fixed `requests[shortKey]` array slot, and one into the `requestsOverflow` mapping:

```solidity
function findRequest(uint64 sequenceNumber) internal view returns (Request storage req) {
    (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);
    req = _state.requests[shortKey];
    if (req.sequenceNumber == sequenceNumber) {
        return req;
    } else {
        req = _state.requestsOverflow[key];   // cold SLOAD per unique seq
    }
}
``` [2](#0-1) 

The `requests` array is fixed at `NUM_REQUESTS = 32` slots; all other requests overflow to a mapping. [3](#0-2) 

After the first 32 iterations the array slots are warm (100 gas each), but every mapping slot is cold (2,100 gas). For **N** fulfilled requests between `firstUnfulfilledSeq` and the next active request, the loop costs approximately:

```
32 × 4,200 + (N − 32) × 2,200 ≈ N × 2,200 gas
```

At N ≈ 13,600 the loop alone consumes ~30 M gas, exceeding the Ethereum block gas limit.

The developers already acknowledge the problem in a TODO comment:

> "I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number. a better solution would be a doubly-linked list of active requests." [4](#0-3) 

**Attack path (no privileged access required):**

1. Victim submits `requestPriceUpdatesWithCallback` → gets sequence number **S**.
2. Attacker submits ~14,000 requests → sequence numbers **S+1 … S+N**.
3. Attacker calls `executeCallback` for each of **S+1 … S+N** (valid Pyth price data is publicly available from Hermes; `executeCallback` has no caller restriction).
4. `firstUnfulfilledSeq` remains at **S** (victim's request is still active).
5. When anyone attempts to fulfill the victim's request **S**, the `while` loop must scan all N inactive slots → OOG exception.
6. The OOG revert undoes `clearRequest(S)`, so the request stays active and can **never** be fulfilled.
7. The victim's fee is permanently locked in the contract.

`executeCallback` has no caller access control, so the attacker can fulfill their own requests freely: [5](#0-4) 

---

### Impact Explanation
**Medium.** No ETH is directly stolen, but the victim's request fee is permanently locked in the contract and the callback is never executed. The contract state becomes incorrect: `firstUnfulfilledSeq` can never advance past the stuck sequence number, degrading all future `executeCallback` calls that try to update it.

---

### Likelihood Explanation
**Medium.** The attacker must pay fees for ~14,000 requests plus gas for ~14,000 `executeCallback` calls. This is economically costly but feasible for a targeted griefing attack (e.g., blocking a high-value callback). The vulnerability also degrades naturally under high out-of-order fulfillment load without any attacker, since the loop is O(gap) with no cap.

---

### Recommendation

1. **Cap the loop iterations** — add a maximum step count per call so the loop cannot exceed a safe gas budget:
   ```solidity
   uint256 maxSteps = 1000;
   while (
       maxSteps-- > 0 &&
       _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
       !isActive(findRequest(_state.firstUnfulfilledSeq))
   ) {
       _state.firstUnfulfilledSeq++;
   }
   ```
2. **Replace the linear scan with a doubly-linked list** of active requests (as the TODO comment already suggests), giving O(1) advancement of `firstUnfulfilledSeq`.
3. **Move the `while` loop after the `try/catch`** so that an OOG in the scan does not revert `clearRequest` and permanently lock funds.

---

### Proof of Concept

```solidity
// 1. Victim creates request at seq S
uint64 S = echo.requestPriceUpdatesWithCallback{value: fee}(
    provider, publishTime, priceIds, callbackGasLimit
);

// 2. Attacker creates 14_000 requests (S+1 … S+14000)
for (uint i = 0; i < 14_000; i++) {
    echo.requestPriceUpdatesWithCallback{value: fee}(
        provider, publishTime, priceIds, callbackGasLimit
    );
}

// 3. Attacker fulfills all 14_000 requests (valid updateData from Hermes)
for (uint64 seq = S + 1; seq <= S + 14_000; seq++) {
    echo.executeCallback(provider, seq, updateData, priceIds);
}
// firstUnfulfilledSeq is still S

// 4. Anyone tries to fulfill victim's request S → OOG, reverts
// clearRequest(S) is also reverted → S remains active forever
// Victim's funds are permanently locked
echo.executeCallback{gas: 30_000_000}(provider, S, updateData, priceIds);
// ↑ reverts with out-of-gas
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-111)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L164-174)
```text
        clearRequest(sequenceNumber);

        // TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
        // a better solution would be a doubly-linked list of active requests.
        // After successful callback, update firstUnfulfilledSeq if needed
        while (
            _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
            !isActive(findRequest(_state.firstUnfulfilledSeq))
        ) {
            _state.firstUnfulfilledSeq++;
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L310-321)
```text
    function findRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            return req;
        } else {
            req = _state.requestsOverflow[key];
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L66-68)
```text
        Request[NUM_REQUESTS] requests;
        mapping(bytes32 => Request) requestsOverflow;
        mapping(address => ProviderInfo) providers;
```
