### Title
Unrestricted First-Call Initialization of Stake Cap Parameters via `init_if_needed` — (`pythnet/stake_caps_parameters/programs/stake_caps_parameters/src/lib.rs`)

---

### Summary

The `set_parameters` instruction in the `stake_caps_parameters` program uses `init_if_needed` to create the `parameters` PDA. The authority guard relies on `current_authority == Pubkey::default()` as the "uninitialized" sentinel, but because `init_if_needed` zero-initializes the account on first creation, **any unprivileged signer can call `set_parameters` before the legitimate authority and set arbitrary `m`, `z`, and `current_authority` values**, permanently seizing control of the OIS pool cap parameters.

---

### Finding Description

In `set_parameters`, the account constraint is:

```rust
#[account(
    init_if_needed,
    seeds = ["parameters".as_bytes()],
    bump,
    payer = signer,
    space = Parameters::LEN
)]
pub parameters: Account<'info, Parameters>,
``` [1](#0-0) 

The authority check inside the instruction handler is:

```rust
require!(
    ctx.accounts.signer.key() == stored_parameters.current_authority
        || stored_parameters.current_authority == Pubkey::default(),
    ErrorCode::WrongAuthority
);
**stored_parameters = parameters;
``` [2](#0-1) 

When `init_if_needed` allocates the account for the first time, Anchor zero-initializes all fields. `current_authority` is therefore `Pubkey::default()` (all zeros). The second branch of the `require!` passes unconditionally for **any** signer, and the entire `Parameters` struct is immediately overwritten with attacker-supplied values — including a new `current_authority` of the attacker's choosing.

After this first call, the legitimate Pyth authority is permanently locked out: the attacker now owns `current_authority` and is the only one who can call `set_parameters` again.

---

### Impact Explanation

`m` (M) and `z` (Z) are the two global constants that govern the OIS pool cap formula for every publisher:

$$C_p = M \cdot \sum_{s \in \text{Symbols}_p} \frac{1}{\max(n_s, Z)}$$ [3](#0-2) 

These values are read by the staking frontend and the Pythnet off-chain cap computation: [4](#0-3) 

An attacker who controls these parameters can:

- Set `m = 0` → all pool caps become zero, no staking rewards can be earned by any publisher or delegator.
- Set `m` to `u64::MAX` → pool caps overflow or become astronomically large, breaking reward accounting.
- Set `z = 0` → if any symbol has zero publishers, the formula divides by zero, causing panics or undefined behavior in off-chain cap computation.
- Set `current_authority` to an address they control → permanently prevent Pyth from correcting the values.

The net effect is a complete disruption of the Oracle Integrity Staking reward and cap system for all participants.

---

### Likelihood Explanation

The vulnerability is exploitable exactly once: before the `parameters` PDA is first initialized on Pythnet. Any unprivileged transaction sender who monitors the Pythnet mempool (or simply races the deployment) can submit `set_parameters` with a malicious `Parameters` struct. No special privileges, leaked keys, or governance majority are required — only a funded Solana keypair and knowledge of the program ID (`ujSFv8q8woXW5PUnby52PQyxYGUudxkrvgN6A631Qmm`). [5](#0-4) 

---

### Recommendation

Replace `init_if_needed` with `init` and add a separate, privileged `initialize` instruction that can only be called once. Alternatively, enforce a hard-coded deployer/governance key as the only permitted first-caller by adding an explicit constraint on `signer` when `current_authority == Pubkey::default()`.

```rust
// Option A: use init (one-time creation only)
#[account(
    init,
    seeds = ["parameters".as_bytes()],
    bump,
    payer = signer,
    space = Parameters::LEN
)]
pub parameters: Account<'info, Parameters>,
```

This mirrors the fix applied in the referenced Otter Audits report (commit e565006) for the analogous `restaking` program.

---

### Proof of Concept

```rust
// Attacker calls set_parameters before Pyth does.
// parameters PDA does not yet exist → init_if_needed creates it with zeroed data.
// current_authority == Pubkey::default() → authority check passes.
// Attacker sets m=0, z=0, current_authority=attacker_pubkey.

let attacker_params = Parameters {
    current_authority: attacker_keypair.pubkey(), // seize authority
    m: 0,   // zero out all pool caps
    z: 0,   // cause division-by-zero in cap formula
};

let ix = Instruction {
    program_id: stake_caps_parameters::id(),
    accounts: stake_caps_parameters::accounts::SetParameters {
        signer: attacker_keypair.pubkey(),
        parameters: PARAMETERS_ADDRESS,  // PDA not yet initialized
        system_program: solana_sdk::system_program::id(),
    }.to_account_metas(None),
    data: stake_caps_parameters::instruction::SetParameters {
        parameters: attacker_params,
    }.data(),
};
// After this tx: Pyth's legitimate authority is locked out permanently.
// All OIS pool caps are zero; no staking rewards can be distributed.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** pythnet/stake_caps_parameters/programs/stake_caps_parameters/src/lib.rs (L3-5)
```rust
declare_id!("ujSFv8q8woXW5PUnby52PQyxYGUudxkrvgN6A631Qmm");

pub const PARAMETERS_ADDRESS: Pubkey = pubkey!("879ZVNagiWaAKsWDjGVf8pLq1wUBeBz7sREjUh3hrU36");
```

**File:** pythnet/stake_caps_parameters/programs/stake_caps_parameters/src/lib.rs (L11-20)
```rust
    pub fn set_parameters(ctx: Context<SetParameters>, parameters: Parameters) -> Result<()> {
        let stored_parameters = &mut ctx.accounts.parameters;
        require!(
            ctx.accounts.signer.key() == stored_parameters.current_authority
                || stored_parameters.current_authority == Pubkey::default(),
            ErrorCode::WrongAuthority
        );
        **stored_parameters = parameters;
        Ok(())
    }
```

**File:** pythnet/stake_caps_parameters/programs/stake_caps_parameters/src/lib.rs (L23-36)
```rust
#[derive(Accounts)]
pub struct SetParameters<'info> {
    #[account(mut)]
    pub signer: Signer<'info>,
    #[account(
        init_if_needed,
        seeds = ["parameters".as_bytes()],
        bump,
        payer = signer,
        space = Parameters::LEN
    )]
    pub parameters: Account<'info, Parameters>,
    pub system_program: Program<'info, System>,
}
```

**File:** apps/developer-hub/content/docs/oracle-integrity-staking/mathematical-representation.mdx (L19-29)
```text
$$
\large{\text{Pool Cap}: {\bold{C_p}} = M \cdot \sum_{s \in \text{Symbols}_p} \frac{1}{\max(n_s, Z)}}
$$

Where:

- $M$ is a constant parameter representing the target stake per symbol.
- $\text{Symbols}_p$ is the set of symbols published by the publisher $p$.
- $n_s$ be the number of publishers for symbol $s$.
- $Z$ is a constant parameter to control cap contribution from symbols with a low number of publishers.

```

**File:** apps/staking/src/api.ts (L233-240)
```typescript
  return {
    currentEpoch,
    m: parameters.m,
    publishers,
    walletAmount,
    yieldRate: poolConfig.y,
    z: parameters.z,
  };
```
