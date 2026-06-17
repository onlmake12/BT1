### Title
Staking Program Lacks Contract Whitelisting, Allowing Governance Voting Power to Be Made Liquid via PDA-Owned Stake Accounts — (`governance/pyth_staking_sdk/src/idl/staking.json`)

---

### Summary

The Pyth staking program's `create_stake_account` instruction is explicitly "trustless" and accepts any arbitrary `owner` pubkey — including a Program Derived Address (PDA) controlled by a third-party Solana program. Because `join_dao_llc` and `create_position` only require the `owner` to sign (which a program can satisfy via `invoke_signed` CPI), a "PYTH Locker" program can be deployed that stakes PYTH tokens for governance on behalf of depositors and then tokenizes or sells the ownership of those staked positions. This makes governance voting power effectively liquid and transferable, directly analogous to the hPAL/veToken locker pattern described in the reference report.

---

### Finding Description

The `create_stake_account` instruction accepts an arbitrary `owner: pubkey` argument with no restriction on whether that pubkey is a wallet or a program-controlled PDA: [1](#0-0) 

The instruction is explicitly documented as "Trustless instruction that creates a stake account for a user." The `payer` must sign, but the `owner` field is a free argument — any pubkey, including a PDA, can be set as owner.

The two gating instructions that precede governance staking are `join_dao_llc` and `create_position`. Both require the `owner` to be a signer: [2](#0-1) [3](#0-2) 

In Solana, a program can satisfy the `signer: true` constraint for a PDA it controls by using `invoke_signed` with the PDA's seeds. There is no `is_signer` check that distinguishes a wallet keypair from a program-derived signer. The `join_dao_llc` check is purely a bytes comparison of `agreement_hash`: [4](#0-3) 

A malicious "PYTH Locker" program can therefore:
1. Create a stake account with a PDA as `owner`.
2. Call `join_dao_llc` via CPI (`invoke_signed`) — passing the correct `agreement_hash` bytes satisfies the check programmatically.
3. Call `create_position` with `{ voting: {} }` via CPI to stake for governance.
4. Implement its own internal ownership ledger (e.g., an NFT or receipt token) representing a user's share of the staked position.
5. Allow users to buy/sell these receipt tokens, making the governance voting power liquid and transferable.

The program's upgrade authority (or an internal admin key) can also be sold, transferring control of the entire staked pool.

---

### Impact Explanation

- **Governance integrity**: Governance voting power, which is intended to be tied to individual token holders who personally agree to the DAO LLC, can be aggregated and sold by a third-party locker program. A single operator can accumulate disproportionate voting power.
- **DAO LLC agreement bypass**: The `join_dao_llc` requirement is meant to ensure each staker personally agrees to the Pyth DAO LLC operating agreement. A program signing this via CPI on behalf of depositors circumvents the legal intent of this gate.
- **Voting power concentration**: As seen with Convex/Curve, a dominant locker can capture a majority of governance votes, allowing the locker operator to unilaterally control protocol decisions.

---

### Likelihood Explanation

This is a well-known attack pattern in DeFi (veToken lockers, liquid staking derivatives). The Pyth staking program provides no on-chain mechanism to prevent it. Any developer familiar with Solana CPI and PDA signing can deploy such a locker. The economic incentive (capturing governance power and earning fees from depositors) is strong. Likelihood is **high** given the precedent and the absence of any guard.

---

### Recommendation

Implement a check in `create_stake_account`, `join_dao_llc`, and `create_position` that verifies the `owner` account is not a PDA (i.e., it lies on the ed25519 curve). In Solana, this can be done with:

```rust
require!(
    owner.is_on_curve(),
    StakingError::OwnerMustBeWallet
);
```

This mirrors the veCRV/veANGLE approach of blocking smart contracts from locking, while still allowing them to interact with non-locking staking operations if desired. Alternatively, implement an explicit whitelist of approved program-controlled stakers (e.g., for future official liquid staking integrations).

---

### Proof of Concept

1. Deploy a Solana program `PythLocker` with a PDA `[b"locker"]` as the designated stake account owner.
2. Call `create_stake_account(owner = PDA, lock = { fullyVested: {} })` — `payer` signs, `owner` is the PDA (no signature required at this step).
3. Call `join_dao_llc(agreement_hash = <correct_hash>)` via CPI from `PythLocker` using `invoke_signed` with seeds `[b"locker"]` — the PDA satisfies `signer: true`.
4. Call `create_position({ voting: {} }, amount)` via CPI similarly — the PDA satisfies `owner signer` constraint.
5. `PythLocker` mints a receipt token to each depositor proportional to their PYTH contribution.
6. Receipt tokens are freely tradeable on any DEX, making the governance voting power liquid.
7. The `PythLocker` operator votes with the full aggregated stake, or sells the program's upgrade authority to transfer control of the entire pool. [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L752-772)
```json
      "args": [
        {
          "name": "owner",
          "type": "pubkey"
        },
        {
          "name": "lock",
          "type": {
            "defined": {
              "name": "VestingSchedule"
            }
          }
        }
      ],
      "discriminator": [105, 24, 131, 19, 201, 250, 157, 73],
      "docs": [
        "Trustless instruction that creates a stake account for a user",
        "The main account i.e. the position accounts needs to be initialized outside of the program",
        "otherwise we run into stack limits"
      ],
      "name": "create_stake_account"
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L971-977)
```json
    {
      "accounts": [
        {
          "name": "owner",
          "relations": ["stake_account_metadata"],
          "signer": true
        },
```

**File:** governance/pyth_staking_sdk/src/idl/staking.json (L1012-1024)
```json
      "args": [
        {
          "name": "_agreement_hash",
          "type": {
            "array": ["u8", 32]
          }
        }
      ],
      "discriminator": [79, 241, 203, 177, 232, 143, 124, 14],
      "docs": [
        "* Accept to join the DAO LLC\n     * This must happen before create_position or update_voter_weight\n     * The user signs a hash of the agreement and the program checks that the hash matches the\n     * agreement"
      ],
      "name": "join_dao_llc"
```

**File:** governance/pyth_staking_sdk/src/types/staking.ts (L374-386)
```typescript
      name: "createPosition";
      docs: [
        "Creates a position",
        "Looks for the first available place in the array, fails if array is full",
        "Computes risk and fails if new positions exceed risk limit",
      ];
      discriminator: [48, 215, 197, 153, 96, 203, 180, 133];
      accounts: [
        {
          name: "owner";
          writable: true;
          signer: true;
          relations: ["stakeAccountMetadata"];
```

**File:** governance/pyth_staking_sdk/src/types/staking.ts (L492-498)
```typescript
      name: "createStakeAccount";
      docs: [
        "Trustless instruction that creates a stake account for a user",
        "The main account i.e. the position accounts needs to be initialized outside of the program",
        "otherwise we run into stack limits",
      ];
      discriminator: [105, 24, 131, 19, 201, 250, 157, 73];
```

**File:** governance/pyth_staking_sdk/src/types/staking.ts (L836-845)
```typescript
      name: "joinDaoLlc";
      docs: [
        "* Accept to join the DAO LLC\n     * This must happen before create_position or update_voter_weight\n     * The user signs a hash of the agreement and the program checks that the hash matches the\n     * agreement",
      ];
      discriminator: [79, 241, 203, 177, 232, 143, 124, 14];
      accounts: [
        {
          name: "owner";
          signer: true;
          relations: ["stakeAccountMetadata"];
```
