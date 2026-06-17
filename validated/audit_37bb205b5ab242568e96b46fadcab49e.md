### Title
`payable(msg.sender).transfer()` in `verifyUpdate` DoS-es Smart Contract Callers — (File: `lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate` uses the deprecated `.transfer()` primitive to refund excess ETH to the caller. `.transfer()` forwards only 2300 gas. Any smart contract whose `receive()` / `fallback()` consumes more than 2300 gas (or has none at all) will have every `verifyUpdate` call revert whenever `msg.value > verification_fee`, permanently blocking that integrator from using the Lazer verification system.

### Finding Description
In `PythLazer.sol`, the `verifyUpdate` function is the sole entry point for verifying Lazer price-update payloads on-chain:

```solidity
// lazer/contracts/evm/src/PythLazer.sol  line 70-76
function verifyUpdate(
    bytes calldata update
) external payable returns (bytes calldata payload, address signer) {
    require(msg.value >= verification_fee, "Insufficient fee provided");
    if (msg.value > verification_fee) {
        payable(msg.sender).transfer(msg.value - verification_fee);  // ← root cause
    }
``` [1](#0-0) 

`address.transfer()` hard-caps the gas forwarded to the recipient at **2300**. Since EIP-1884 (Istanbul), many common operations (e.g., a single `SLOAD`) already cost 2100 gas, so any smart contract with a non-trivial `receive()` or `fallback()` — or with no `receive()` at all — will cause `.transfer()` to revert. Because the revert propagates up through `verifyUpdate`, the entire call fails and the caller's ETH is returned, but the price-update verification never completes.

The `verification_fee` is owner-settable and starts at 1 wei:

```solidity
// lazer/contracts/evm/src/PythLazer.sol  line 26
verification_fee = 1 wei;
``` [2](#0-1) 

A smart contract integrator that sends any amount above the exact current fee — a natural defensive pattern when fees can change — will be permanently unable to call `verifyUpdate` if its `receive()` function uses more than 2300 gas.

### Impact Explanation
`verifyUpdate` is the **only** function in `PythLazer` that verifies a Lazer price update. A smart contract integrator that is blocked from calling it cannot consume Lazer prices at all. Because the integrator cannot change its own `receive()` function after deployment, the DoS is permanent for that contract. No funds are locked, but the core protocol service is rendered inaccessible to an entire class of callers.

### Likelihood Explanation
- Smart contracts routinely have non-trivial `receive()` functions (event emission, state writes, etc.), all of which exceed 2300 gas.
- Defensive callers commonly overpay fees to tolerate future fee increases; the owner can raise `verification_fee` at any time, making exact-fee calculation unreliable.
- The entry path requires no privilege: any unprivileged Lazer updater or integrating contract can trigger this condition simply by calling `verifyUpdate{value: verification_fee + 1}(...)`.

### Recommendation
Replace `.transfer()` with a low-level `.call{value: ...}("")` and check the return value, which forwards all available gas:

```solidity
if (msg.value > verification_fee) {
    (bool ok, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(ok, "Refund failed");
}
```

Alternatively, adopt a pull-payment pattern: track excess ETH per caller and let them withdraw it separately, eliminating the external call from the hot path entirely.

### Proof of Concept
1. Deploy a contract `Integrator` with a `receive()` that emits an event (costs >2300 gas).
2. `Integrator` calls `PythLazer.verifyUpdate{value: 2 wei}(validUpdate)` while `verification_fee == 1 wei`.
3. Inside `verifyUpdate`, `payable(msg.sender).transfer(1 wei)` is executed.
4. The EVM forwards only 2300 gas to `Integrator.receive()`. The event emission exhausts the stipend; `receive()` reverts.
5. The revert propagates: `verifyUpdate` reverts entirely.
6. `Integrator` can never successfully call `verifyUpdate` with any `msg.value > verification_fee`, permanently blocking it from verifying Lazer price updates. [3](#0-2)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L26-26)
```text
        verification_fee = 1 wei;
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
