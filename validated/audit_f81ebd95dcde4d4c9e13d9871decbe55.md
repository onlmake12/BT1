### Title
Entropy Keeper EOA Funds at Risk via `tx.origin` Check in Malicious `_entropyCallback` — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`revealWithCallback` in `Entropy.sol` is callable by anyone and triggers a call into a user-controlled contract (`req.requester._entropyCallback`). Because the keeper's EOA is `tx.origin` during this callback, a malicious requester contract can exploit any third-party contract that uses `tx.origin`-based authorization to drain the keeper's funds without their consent.

---

### Finding Description

`revealWithCallback` is explicitly documented as callable by anyone:

> "Anyone can call this method to fulfill a request, but the callback will only be made to the original requester." [1](#0-0) 

After validating the provider's proof, the function calls into `req.requester` — a user-controlled contract address — via two code paths:

**Path 1 (gasLimit10k != 0, CALLBACK_NOT_STARTED):** Uses `excessivelySafeCall` to invoke `_entropyCallback` on the requester contract: [2](#0-1) 

**Path 2 (legacy / no gas limit):** Directly calls `IEntropyConsumer(callAddress)._entropyCallback(...)`: [3](#0-2) 

In both paths, `tx.origin` during the callback execution is the keeper's EOA — the address that submitted the `revealWithCallback` transaction. The Entropy contract does not sanitize or restrict what the requester contract does inside `_entropyCallback`.

**Attack scenario:**

1. Attacker deploys a malicious contract `MaliciousConsumer` that:
   - Calls `requestWithCallback` (or `requestV2`) to register itself as `req.requester`.
   - Implements `_entropyCallback` to call a third-party contract (e.g., a DeFi protocol, a token approval contract, or any contract where the keeper holds a balance or has granted approvals) that checks `tx.origin == keeper_EOA` and executes a withdrawal or transfer on behalf of the keeper.
2. The keeper's off-chain service detects the pending request and calls `revealWithCallback`.
3. During the callback, `tx.origin` is the keeper's EOA.
4. The malicious `_entropyCallback` calls the third-party contract, which authorizes the action based on `tx.origin`, draining the keeper's funds.

Note that `excessivelySafeCall` (Path 1) limits return data copying but does **not** prevent `tx.origin` from being visible to the callee or any contracts it calls. [4](#0-3) 

---

### Impact Explanation

A keeper/provider EOA that holds funds or has token approvals in any third-party contract that uses `tx.origin`-based authorization can have those funds drained without their permission. The keeper is unaware that the requester contract is malicious, since the callback target is determined solely by who called `requestWithCallback` — an unprivileged, permissionless operation.

This is a direct loss of funds for the keeper, not for the Pyth protocol itself. The impact is bounded by what the keeper's EOA holds or has approved in `tx.origin`-checking contracts.

---

### Likelihood Explanation

- **Entry path is fully permissionless**: Any address can call `requestWithCallback` and register an arbitrary contract as the requester.
- **Keeper behavior is predictable**: Keepers are automated bots that fulfill all pending requests; they will call `revealWithCallback` for any valid pending request.
- **Attack requires no privileged access**: The attacker only needs to pay the entropy fee (a small amount) to register the malicious request.
- **`tx.origin` checks exist in real DeFi contracts**: Protocols that use `tx.origin` for deposit/withdrawal authorization (e.g., some older or custom contracts) are realistic targets.

Likelihood is **medium** — the attack requires a keeper EOA that also interacts with a `tx.origin`-checking contract, which is not universal but is realistic for keepers that reuse their EOA for other DeFi activity.

---

### Recommendation

1. **Document that keeper/provider EOAs must be single-purpose**: Keepers should use a dedicated EOA with no other DeFi activity, no token approvals, and no balances in third-party contracts that use `tx.origin`-based authorization.
2. **Warn in the keeper documentation** that the `_entropyCallback` of the requester contract is untrusted code and that `tx.origin` will be the keeper's EOA during its execution.
3. Optionally, consider whether a wrapper contract (rather than a raw EOA) should be recommended for keepers, so that `tx.origin` is not a meaningful address in any third-party context.

---

### Proof of Concept

```solidity
// Attacker's malicious requester contract
contract MaliciousConsumer is IEntropyConsumer {
    IEntropy entropy;
    address thirdPartyVault; // a contract that checks tx.origin for withdrawals

    constructor(address _entropy, address _vault) {
        entropy = IEntropy(_entropy);
        thirdPartyVault = _vault;
    }

    function triggerAttack(address provider) external payable {
        uint256 fee = entropy.getFee(provider);
        // Register this contract as the requester
        entropy.requestWithCallback{value: fee}(provider, bytes32(uint256(42)));
    }

    // Called by Entropy during revealWithCallback — tx.origin == keeper EOA
    function _entropyCallback(
        uint64 sequenceNumber,
        address provider,
        bytes32 randomNumber
    ) internal override {
        // tx.origin is the keeper's EOA here.
        // Call a third-party contract that uses tx.origin for authorization.
        IVault(thirdPartyVault).withdrawAll(); // drains keeper's funds if vault checks tx.origin
    }

    function getEntropy() internal view override returns (address) {
        return address(entropy);
    }
}
```

The keeper calls `revealWithCallback` on the pending request. During `_entropyCallback`, `tx.origin` is the keeper's EOA. `IVault.withdrawAll()` checks `tx.origin` and transfers the keeper's deposited funds to the attacker. [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L541-547)
```text
    // Anyone can call this method to fulfill a request, but the callback will only be made to the original requester.
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L568-600)
```text
        // If the request has an explicit gas limit, then run the new callback failure state flow.
        //
        // Requests that haven't been invoked yet will be invoked safely (catching reverts), and
        // any reverts will be reported as an event. Any failing requests move to a failure state
        // at which point they can be recovered. The recovery flow invokes the callback directly
        // (no catching errors) which allows callers to easily see the revert reason.
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
            req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
            bool success;
            bytes memory ret;
            uint256 startingGas = gasleft();
            (success, ret) = req.requester.excessivelySafeCall(
                // Warning: the provided gas limit below is only an *upper bound* on the gas provided to the call.
                // At most 63/64ths of the current context's gas will be provided to a call, which may be less
                // than the indicated gas limit. (See CALL opcode docs here https://www.evm.codes/?fork=cancun#f1)
                // Consequently, out-of-gas reverts need to be handled carefully to ensure that the callback
                // was truly provided with a sufficient amount of gas.
                uint256(req.gasLimit10k) * TEN_THOUSAND,
                256, // copy at most 256 bytes of the return value into ret.
                abi.encodeWithSelector(
                    IEntropyConsumer._entropyCallback.selector,
                    sequenceNumber,
                    provider,
                    randomNumber
                )
            );
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());
            // Reset status to not started here in case the transaction reverts.
            req.callbackStatus = EntropyStatusConstants.CALLBACK_NOT_STARTED;

```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-681)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```
