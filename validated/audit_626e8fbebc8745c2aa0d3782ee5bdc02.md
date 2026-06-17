### Title
Predictable On-Chain User Contribution in `requestV2()` Convenience Methods Allows Provider to Pre-Compute and Selectively Fulfill Random Number Requests — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `random()` internal function in `Entropy.sol` generates the user's contribution to the commit-reveal protocol using only publicly observable on-chain values (`block.timestamp`, `block.prevrandao`, `msg.sender`, `_state.seed`). When users call the no-argument `requestV2()` convenience overloads, their user contribution is silently replaced with this predictable value. Because all inputs to `random()` are public, a dishonest provider can compute the user's contribution before deciding whether to fulfill the request, breaking the core security guarantee of the Entropy protocol.

---

### Finding Description

The Entropy protocol's security model is explicitly stated in the contract's own comments:

> "This protocol has the property that the result is random as long as either A or B are honest."

The user's contribution is the mechanism that protects against a dishonest provider: if the user keeps their contribution secret until after the provider commits, the provider cannot bias the outcome.

Three public `requestV2()` overloads bypass this protection by calling `random()` to auto-generate the user contribution:

```solidity
// Entropy.sol lines 286-310
function requestV2() external payable override returns (uint64) {
    assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
}
function requestV2(uint32 gasLimit) external payable override returns (uint64) {
    assignedSequenceNumber = requestV2(getDefaultProvider(), random(), gasLimit);
}
function requestV2(address provider, uint32 gasLimit) external payable override returns (uint64) {
    assignedSequenceNumber = requestV2(provider, random(), gasLimit);
}
```

The `random()` function is:

```solidity
// Entropy.sol lines 1079-1089
function random() internal returns (bytes32) {
    _state.seed = keccak256(
        abi.encodePacked(
            block.timestamp,
            block.prevrandao,
            msg.sender,
            _state.seed
        )
    );
    return _state.seed;
}
```

Every input is publicly observable:
- `block.timestamp` — public, slightly manipulable by validators
- `block.prevrandao` — the beacon chain RANDAO value, public before the block is finalized
- `msg.sender` — public
- `_state.seed` — stored in contract state, readable by anyone

A provider monitoring the mempool can:
1. Observe a pending `requestV2()` transaction
2. Simulate the transaction to compute the exact output of `random()` (the user's contribution)
3. Compute the final random number: `keccak256(userContribution, providerContribution_i, 0)` — the provider already knows `providerContribution_i` because they generated the hash chain, and `useBlockhash = false` for all `requestV2` paths
4. Decide whether to fulfill the request based on the pre-computed outcome

The `revealWithCallback` function confirms the provider supplies `providerContribution` at reveal time:

```solidity
// Entropy.sol lines 542-547
function revealWithCallback(
    address provider,
    uint64 sequenceNumber,
    bytes32 userContribution,
    bytes32 providerContribution
) public override {
```

Since the provider knows both `userContribution` (predictable) and `providerContribution` (their own hash chain value), they can compute the final random number before calling `revealWithCallback`.

---

### Impact Explanation

Applications that use the `requestV2()` convenience methods — lotteries, NFT trait randomness, on-chain games — are silently reduced from the two-party security model to a single-party model where the provider alone determines the outcome. A dishonest provider can:

- **Selectively delay fulfillment** for requests whose pre-computed outcome is unfavorable (e.g., the user would win a jackpot), waiting until the request expires or the user gives up
- **Selectively fulfill** only requests with outcomes favorable to the provider or a colluding party
- **Front-run downstream applications** with advance knowledge of the random number before the callback fires

This is a direct violation of the protocol's stated security guarantee and can result in loss of funds for users of any application built on the `requestV2()` convenience API.

---

### Likelihood Explanation

The `requestV2()` convenience methods are the primary integration path advertised for developers who do not want to manage their own randomness. Any provider registered with the Entropy contract — a permissionless operation — can execute this attack. No privileged access, leaked key, or governance majority is required. The provider is explicitly modeled as an untrusted party in the Entropy security design, so a dishonest provider is a realistic and in-scope attacker.

---

### Recommendation

The `random()` function must not be used to generate the user's contribution. The user contribution must be a secret value known only to the user prior to the provider's reveal. Options:

1. **Remove the convenience overloads** that call `random()` internally and require callers to always supply their own `userContribution`.
2. **If a convenience API is required**, document clearly that these overloads provide weaker security guarantees (provider-trust-only) and should not be used in high-value applications.
3. **If on-chain generation is desired**, use a commit-then-reveal approach where the user contribution is committed in a prior block and cannot be predicted by the provider at reveal time.

---

### Proof of Concept

**Setup**: Attacker registers as a provider with a known hash chain `[x_N, x_{N-1}, ..., x_0]`.

**Step 1**: Monitor the mempool for a `requestV2()` call targeting the attacker's provider address.

**Step 2**: Simulate the transaction locally to compute:
```
userContribution = keccak256(block.timestamp || block.prevrandao || msg.sender || _state.seed)
```
All values are public at mempool observation time.

**Step 3**: Compute the assigned sequence number `i` (deterministic from `providerInfo.sequenceNumber`), then compute:
```
finalRandom = keccak256(userContribution || x_i || bytes32(0))
```
(`useBlockhash = false` for all `requestV2` paths, confirmed at line 369)

**Step 4**: If `finalRandom` is unfavorable (e.g., user wins a lottery), do not call `revealWithCallback`. If favorable, call `revealWithCallback` immediately.

**Result**: The provider has full control over whether to deliver a favorable or unfavorable outcome, defeating the two-party randomness guarantee. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L286-310)
```text
    function requestV2()
        external
        payable
        override
        returns (uint64 assignedSequenceNumber)
    {
        assignedSequenceNumber = requestV2(getDefaultProvider(), random(), 0);
    }

    function requestV2(
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(
            getDefaultProvider(),
            random(),
            gasLimit
        );
    }

    function requestV2(
        address provider,
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(provider, random(), gasLimit);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L362-370)
```text
    ) public payable override returns (uint64) {
        EntropyStructsV2.Request storage req = requestHelper(
            provider,
            constructUserCommitment(userContribution),
            // If useBlockHash is set to true, it allows a scenario in which the provider and miner can collude.
            // If we remove the blockHash from this, the provider would have no choice but to provide its committed
            // random number. Hence, useBlockHash is set to false.
            false,
            true,
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-548)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.Request storage req = findActiveRequest(
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1079-1089)
```text
    function random() internal returns (bytes32) {
        _state.seed = keccak256(
            abi.encodePacked(
                block.timestamp,
                block.prevrandao,
                msg.sender,
                _state.seed
            )
        );
        return _state.seed;
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L40-42)
```text
        // Seed for in-contract PRNG. This seed is used to generate user random numbers in some callback flows.
        bytes32 seed;
    }
```
