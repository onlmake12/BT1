### Title
`verifyUpdate` Refund via `transfer()` Permanently Blocks Contract Callers Without `receive()` — (`lazer/contracts/evm/src/PythLazer.sol`)

---

### Summary

`PythLazer.verifyUpdate` uses `payable(msg.sender).transfer(...)` to refund excess ETH to the caller. Any contract caller that lacks a `receive()` or `fallback()` function — or whose `receive()` consumes more than 2300 gas — will have every `verifyUpdate` call revert whenever `msg.value > verification_fee`. Because `verification_fee` is a mutable owner-controlled variable, contract integrators that send a small buffer to guard against fee changes are permanently locked out of the Lazer verification service.

---

### Finding Description

In `PythLazer.sol`, the `verifyUpdate` function is:

```solidity
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    require(msg.value >= verification_fee, "Insufficient fee provided");
    if (msg.value > verification_fee) {
        payable(msg.sender).transfer(msg.value - verification_fee);  // line 76
    }
    ...
}
``` [1](#0-0) 

`transfer()` forwards exactly 2300 gas and reverts on failure. If `msg.sender` is a contract without a `receive()` or `fallback()` function, the EVM will revert the entire `verifyUpdate` call. The same failure occurs for contracts whose `receive()` logic exceeds 2300 gas (e.g., contracts that emit events or write storage on ETH receipt).

`verification_fee` is a plain storage variable initialized to `1 wei` and can be updated by the owner at any time:

```solidity
verification_fee = 1 wei;  // line 26
``` [2](#0-1) 

Because the fee is mutable, a contract integrator that reads `verification_fee` off-chain and sends a small buffer (e.g., `fee + 1 wei`) to guard against a race condition will trigger the refund path. If that integrator contract has no `receive()` function, the call reverts unconditionally.

There is no alternative code path that avoids the refund: the only way to avoid it is to send exactly `verification_fee` wei, which requires atomic on-chain fee reading — not possible for off-chain callers that pre-compute the value.

---

### Impact Explanation

Contract-based Lazer updaters or on-chain integrators that do not implement a `receive()` function are permanently unable to call `verifyUpdate` whenever they send any excess ETH. This is a denial-of-service against the Lazer price verification service for an entire class of callers. Funds sent in the failing transaction are not lost (the revert returns them), but the service is completely inaccessible to those callers unless they can guarantee byte-exact fee payment — which is not reliably achievable in a concurrent environment where the owner can change `verification_fee`.

---

### Likelihood Explanation

Many on-chain contracts (e.g., protocol adapters, multisigs, proxy contracts, and contracts that deliberately reject ETH) do not implement `receive()`. The Lazer SDK and documentation do not warn callers to implement `receive()`. Any such contract that sends even 1 wei over the fee will be permanently blocked. The likelihood is **medium**: it affects a specific but common class of contract callers, and the trigger condition (overpaying by any amount) is easy to hit.

---

### Recommendation

Replace `transfer()` with a low-level `call` for the refund, consistent with the pattern used elsewhere in the Pyth codebase (e.g., `Entropy.sol`, `Echo.sol`, `Scheduler.sol`):

```solidity
if (msg.value > verification_fee) {
    (bool sent, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(sent, "Refund failed");
}
```

Alternatively, require exact payment (`msg.value == verification_fee`) and remove the refund path entirely.

---

### Proof of Concept

1. Deploy a contract `NoReceive` with no `receive()` or `fallback()` function that calls `PythLazer.verifyUpdate{value: verification_fee + 1}(update)`.
2. The call reaches line 76 of `PythLazer.sol` and executes `payable(msg.sender).transfer(1)`.
3. The EVM attempts to send 1 wei to `NoReceive` with 2300 gas. Since `NoReceive` has no `receive()` function, the transfer reverts.
4. The entire `verifyUpdate` transaction reverts with no output.
5. `NoReceive` can never successfully call `verifyUpdate` unless it sends exactly `verification_fee` — which is not reliably achievable if the fee changes between the off-chain read and on-chain execution. [3](#0-2)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L22-27)
```text
    function initialize(address _topAuthority) public initializer {
        __Ownable_init(_topAuthority);
        __UUPSUpgradeable_init();

        verification_fee = 1 wei;
    }
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L70-77)
```text
    function verifyUpdate(
        bytes calldata update
    ) external payable returns (bytes calldata payload, address signer) {
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
