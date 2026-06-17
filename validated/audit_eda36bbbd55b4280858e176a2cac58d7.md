### Title
Predictable In-Contract PRNG Seed Enables Provider-Alone Manipulation of Entropy Random Number — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `random()` function in `Entropy.sol` generates the user's contribution to the random number using four inputs that are all either publicly readable from contract storage or predictable from on-chain data. This allows a permissionlessly registered provider — **without any validator collusion** — to predict the user's contribution before the request transaction is mined, enabling selective censorship or manipulation of the final random number delivered to callers of `requestV2()`.

---

### Finding Description

The `random()` function at lines 1079–1089 of `Entropy.sol` is called by the no-argument `requestV2()` overloads to generate the user's contribution `x_U`:

```solidity
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
``` [1](#0-0) 

This output is used directly as the user's contribution when a caller invokes the simplified `requestV2(address provider, uint32 gasLimit)` overload:

```solidity
function requestV2(
    address provider,
    uint32 gasLimit
) external payable override returns (uint64 assignedSequenceNumber) {
    assignedSequenceNumber = requestV2(provider, random(), gasLimit);
}
``` [2](#0-1) 

All four inputs to `random()` are either publicly readable or predictable before the block is finalized:

| Input | Predictability |
|---|---|
| `_state.seed` | Stored in contract storage (`EntropyState._state.seed`); readable via `eth_getStorageAt` at any time |
| `block.prevrandao` | The RANDAO value from the **previous** block — finalized and publicly known before the current block is produced |
| `msg.sender` | The caller's address — known to the provider |
| `block.timestamp` | Validator-set; typically within ±12 seconds of wall-clock time, enumerable over a small candidate set | [3](#0-2) 

The `_state.seed` field is declared `private` in Solidity, but Solidity `private` only prevents other contracts from reading it via ABI; the raw storage slot remains publicly readable via standard RPC calls.

---

### Impact Explanation

The Entropy protocol's security guarantee is that the final random number `r = hash(x_i, x_U)` is unpredictable as long as **either** the provider or the user is honest. When `requestV2()` is used without a caller-supplied random number, the contract substitutes `random()` for `x_U`. Because `random()` is fully predictable by a provider:

1. The provider reads `_state.seed` from storage, reads `block.prevrandao` from the chain head, knows `msg.sender`, and enumerates a small set of candidate `block.timestamp` values.
2. For each candidate, the provider computes `predicted_x_U = keccak256(abi.encodePacked(ts, prevrandao, sender, seed))`.
3. The provider already knows their own `x_i` (from their hash chain). They compute `r = hash(x_i, predicted_x_U)`.
4. If `r` is unfavorable (e.g., the user wins a lottery), the provider withholds `x_i` (censorship attack). If favorable, the provider reveals.

This completely breaks the "either party honest" guarantee for the large class of users who rely on the simplified `requestV2()` API — which is the API promoted in the official documentation and quick-start guides. [4](#0-3) 

---

### Likelihood Explanation

- **Entry path is permissionless**: Any actor can call `register()` on the Entropy contract to become a provider. No privileged access is required.
- **No validator collusion needed**: The documentation states "a dishonest validator and provider can collude," but the actual implementation is weaker — `_state.seed` is publicly readable and `block.prevrandao` is finalized before the current block, so the provider needs only to enumerate `block.timestamp` over a small window (≤ ~25 candidates on Ethereum with 12-second slots).
- **Widely used API surface**: The zero-argument `requestV2()` and `requestV2(uint32 gasLimit)` are the recommended entry points in the official docs and SDK examples, making this the common code path for integrators. [5](#0-4) 

---

### Recommendation

For the `requestV2()` no-user-contribution overloads, replace the `random()` PRNG with a source that cannot be predicted before the request transaction is mined. Concrete options:

1. **Require a user-supplied contribution**: Remove the no-argument overloads or make them emit a warning; direct users to the `requestV2(address, bytes32, uint32)` variant that accepts an explicit `userRandomNumber`.
2. **Commit-then-reveal for the user contribution**: Record a commitment in the request transaction and derive `x_U` from the block hash of a future block (after the request is mined), so the provider cannot know `x_U` at reveal time.
3. **Do not expose `_state.seed` as a predictable chain**: If an in-contract PRNG must be used, mix in a value that is not known until after the request is included (e.g., `blockhash(block.number - 1)` at reveal time, not at request time).

---

### Proof of Concept

```python
# Off-chain prediction by a malicious provider (pseudocode)
seed        = eth_getStorageAt(entropy_contract, SEED_SLOT)   # public storage
prevrandao  = w3.eth.get_block("latest").prevRandao            # known before next block
msg_sender  = user_address                                     # known from mempool

for ts in range(current_time - 12, current_time + 13):
    predicted_xU = keccak256(abi.encodePacked(ts, prevrandao, msg_sender, seed))
    r = keccak256(abi.encodePacked(x_i, predicted_xU))         # x_i from provider's chain
    if is_unfavorable(r):
        # withhold x_i — censorship attack
        pass
    else:
        reveal(provider, sequence_number, predicted_xU, x_i)
```

The provider iterates at most ~25 timestamp candidates. On any chain with deterministic block times (Ethereum PoS, L2s), the candidate set is even smaller (often 1–3 values). The attack requires no special privileges beyond being a registered provider. [1](#0-0) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L305-310)
```text
    function requestV2(
        address provider,
        uint32 gasLimit
    ) external payable override returns (uint64 assignedSequenceNumber) {
        assignedSequenceNumber = requestV2(provider, random(), gasLimit);
    }
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L20-29)
```text
    ///
    /// Note that this method uses an in-contract PRNG to generate the user's contribution to the random number.
    /// This approach modifies the security guarantees such that a dishonest validator and provider can
    /// collude to manipulate the result (as opposed to a malicious user and provider). That is, the user
    /// now trusts the validator honestly draw a random number. If you wish to avoid this trust assumption,
    /// call a variant of `requestV2` that accepts a `userRandomNumber` parameter.
    function requestV2()
        external
        payable
        returns (uint64 assignedSequenceNumber);
```

**File:** apps/developer-hub/content/docs/entropy/request-callback-variants.mdx (L9-31)
```text
## 1. Basic request

```solidity
function requestV2() external payable returns (uint64 assignedSequenceNumber);
```

This is the simplest variant that requests entropy using the default provider and default gas settings. Uses in-contract PRNG for the user contribution to randomness.

Use this when you want the most straightforward implementation and don't need to customize provider or gas parameters.

### Example

```solidity
function requestBasicRandomNumber() external payable {
    // Get the fee for the default provider and gas limit
    uint256 fee = entropy.getFeeV2();

    require(msg.value >= fee, "Insufficient fee");

    // Request randomness with default settings
    uint64 sequenceNumber = entropy.requestV2{value: fee}();
}
```
```
