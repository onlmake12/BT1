### Title
Unsafe `transfer()` for ETH Refund Causes DoS for Smart Contract Callers — (`lazer/contracts/evm/src/PythLazer.sol`)

### Summary
`PythLazer.verifyUpdate()` uses the native `payable(msg.sender).transfer()` to refund excess ETH to callers who overpay the `verification_fee`. Because `transfer()` forwards only 2300 gas, any smart contract caller whose `receive()` or `fallback()` function consumes more than 2300 gas will cause the entire `verifyUpdate()` call to revert, permanently blocking those callers from using the function.

### Finding Description
In `PythLazer.sol`, the `verifyUpdate()` function accepts ETH as a fee and refunds any excess to `msg.sender` using `transfer()`:

```solidity
// lazer/contracts/evm/src/PythLazer.sol, lines 74-77
require(msg.value >= verification_fee, "Insufficient fee provided");
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
``` [1](#0-0) 

`transfer()` stipends exactly 2300 gas to the recipient. This is insufficient for:
- Smart contracts using a proxy pattern (proxy overhead consumes part of the 2300 gas before the implementation's `receive()` is reached).
- Smart contracts whose `receive()` or `fallback()` performs any non-trivial operation (e.g., emitting an event, updating state, or calling another contract).
- Multi-sig wallets and account-abstraction wallets, which are common in DeFi integrations.

When the `transfer()` call reverts, the entire `verifyUpdate()` transaction reverts, meaning the caller receives no price payload and no refund.

### Impact Explanation
Any smart contract that integrates `PythLazer.verifyUpdate()` and sends `msg.value > verification_fee` (e.g., to avoid calculating the exact fee off-chain, or due to fee changes) will have every call revert. This is a denial-of-service against all smart contract Lazer updaters/consumers that are not EOAs. The contract's core functionality — verifying a Lazer price update — becomes completely inaccessible to an entire class of legitimate callers.

### Likelihood Explanation
DeFi protocols and on-chain integrators routinely call oracle update functions from smart contracts. Sending a small buffer above the exact fee is a standard defensive pattern. Proxy-based contracts (e.g., OpenZeppelin `TransparentUpgradeableProxy`, Gnosis Safe) are ubiquitous and their `receive()` paths consume well above 2300 gas. The likelihood that at least one production integrator hits this path is high.

### Recommendation
Replace `transfer()` with a low-level `.call()` and check the return value:

```solidity
if (msg.value > verification_fee) {
    (bool success, ) = payable(msg.sender).call{value: msg.value - verification_fee}("");
    require(success, "ETH refund failed");
}
```

This forwards all remaining gas to the recipient, eliminating the 2300-gas restriction.

### Proof of Concept
1. Deploy a smart contract `Integrator` with a `receive()` function that emits an event (costs ~750 gas) or uses a proxy pattern (adds overhead).
2. Call `integrator.callVerifyUpdate{value: 2 wei}(lazerContract, updateData)` where `verification_fee == 1 wei`.
3. Inside `Integrator.callVerifyUpdate`, call `pythLazer.verifyUpdate{value: 2 wei}(updateData)`.
4. `PythLazer` attempts `payable(integrator).transfer(1 wei)` — the `receive()` in `Integrator` exceeds 2300 gas.
5. The `transfer()` reverts, causing `verifyUpdate()` to revert entirely.
6. `Integrator` never receives the price payload, and the fee is not refunded.

The root cause is exclusively in `PythLazer.sol` line 76. [2](#0-1)

### Citations

**File:** lazer/contracts/evm/src/PythLazer.sol (L74-77)
```text
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
