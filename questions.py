import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the path from https:///github.com/dfinity/ICRC-1
SOURCE_REPO = "pyth-network/pyth-crosschain"
# todo: the name of the repository
REPO_NAME = "pyth-crosschain"
run_number = os.environ.get('GITHUB_RUN_NUMBER') or os.environ.get('CI_PIPELINE_IID', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
scope_files = [
    "lazer/contracts/sui/sources/actions.move",
    "lazer/contracts/sui/sources/channel.move",
    "lazer/contracts/sui/sources/channel_v2.move",
    "lazer/contracts/sui/sources/feed.move",
    "lazer/contracts/sui/sources/governance.move",
    "lazer/contracts/sui/sources/i16.move",
    "lazer/contracts/sui/sources/i64.move",
    "lazer/contracts/sui/sources/market_session.move",
    "lazer/contracts/sui/sources/meta.move",
    "lazer/contracts/sui/sources/parser.move",
    "lazer/contracts/sui/sources/pyth_lazer.move",
    "lazer/contracts/sui/sources/state.move",
    "lazer/contracts/sui/sources/update.move",
    "lazer/contracts/sui/sources/update_v2.move",
    "lazer/contracts/cardano/lib/pyth/governance.ak",
    "lazer/contracts/cardano/lib/secp256k1.ak",
    "lazer/contracts/cardano/lib/state.ak",
    "lazer/contracts/cardano/lib/wormhole/governance.ak",
    "lazer/contracts/cardano/lib/wormhole/vaa.ak",
    "lazer/contracts/cardano/lib/wormhole.ak",
    "lazer/contracts/cardano/validators/pyth_price.ak",
    "lazer/contracts/cardano/validators/pyth_state.ak",
    "lazer/contracts/cardano/validators/wormhole_state.ak",
    "lazer/contracts/evm/src/PythLazer.sol",
    "lazer/contracts/evm/src/PythLazerLib.sol",
    "lazer/contracts/evm/src/PythLazerStructs.sol",
    "target_chains/ethereum/contracts/contracts/entropy/Entropy.sol",
    "target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol",
    "target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol",
    "target_chains/ethereum/contracts/contracts/entropy/EntropyStructConverter.sol",
    "target_chains/ethereum/contracts/contracts/entropy/EntropyUpgradable.sol",
    "target_chains/sui/contracts/sources/batch_price_attestation.move",
    "target_chains/sui/contracts/sources/data_source.move",
    "target_chains/sui/contracts/sources/deserialize.move",
    "target_chains/sui/contracts/sources/event.move",
    "target_chains/sui/contracts/sources/governance/contract_upgrade.move",
    "target_chains/sui/contracts/sources/governance/governance.move",
    "target_chains/sui/contracts/sources/governance/governance_action.move",
    "target_chains/sui/contracts/sources/governance/governance_instruction.move",
    "target_chains/sui/contracts/sources/governance/set_data_sources.move",
    "target_chains/sui/contracts/sources/governance/set_fee_recipient.move",
    "target_chains/sui/contracts/sources/governance/set_governance_data_source.move",
    "target_chains/sui/contracts/sources/governance/set_stale_price_threshold.move",
    "target_chains/sui/contracts/sources/governance/set_update_fee.move",
    "target_chains/sui/contracts/sources/hot_potato_vector.move",
    "target_chains/sui/contracts/sources/i64.move",
    "target_chains/sui/contracts/sources/merkle_tree.move",
    "target_chains/sui/contracts/sources/migrate.move",
    "target_chains/sui/contracts/sources/price.move",
    "target_chains/sui/contracts/sources/price_feed.move",
    "target_chains/sui/contracts/sources/price_identifier.move",
    "target_chains/sui/contracts/sources/price_info.move",
    "target_chains/sui/contracts/sources/price_status.move",
    "target_chains/sui/contracts/sources/pyth.move",
    "target_chains/sui/contracts/sources/pyth_accumulator.move",
    "target_chains/sui/contracts/sources/set.move",
    "target_chains/sui/contracts/sources/setup.move",
    "target_chains/sui/contracts/sources/state.move",
    "target_chains/sui/contracts/sources/version_control.move",
    "target_chains/solana/crates/wormhole-solana/src/accounts/claim.rs",
    "target_chains/solana/crates/wormhole-solana/src/accounts/config.rs",
    "target_chains/solana/crates/wormhole-solana/src/accounts/emitter.rs",
    "target_chains/solana/crates/wormhole-solana/src/accounts/fee_collector.rs",
    "target_chains/solana/crates/wormhole-solana/src/accounts/guardian_set.rs",
    "target_chains/solana/crates/wormhole-solana/src/accounts/sequence.rs",
    "target_chains/solana/crates/wormhole-solana/src/accounts/vaa.rs",
    "target_chains/solana/crates/wormhole-solana/src/accounts.rs",
    "target_chains/solana/crates/wormhole-solana/src/instructions/initialize.rs",
    "target_chains/solana/crates/wormhole-solana/src/instructions/post_message.rs",
    "target_chains/solana/crates/wormhole-solana/src/instructions/post_vaa.rs",
    "target_chains/solana/crates/wormhole-solana/src/instructions/set_fees.rs",
    "target_chains/solana/crates/wormhole-solana/src/instructions/upgrade_guardian_set.rs",
    "target_chains/solana/crates/wormhole-solana/src/instructions/verify_signatures.rs",
    "target_chains/solana/crates/wormhole-solana/src/instructions.rs",
    "target_chains/solana/crates/wormhole-solana/src/lib.rs",
    "target_chains/solana/crates/wormhole-solana/src/message.rs",
    "target_chains/solana/programs/core-bridge/src/constants.rs",
    "target_chains/solana/programs/core-bridge/src/error.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/accounts/mod.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/instruction/mod.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/mod.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/governance/guardian_set_update.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/governance/mod.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/governance/set_message_fee.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/governance/transfer_fees.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/governance/upgrade_contract.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/initialize.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/mod.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/mod.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/post_message/unreliable.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/post_vaa.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/update_guardian_set_ttl.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/processor/verify_signatures.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/state/config.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/state/emitter_sequence.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/state/guardian_set.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/state/mod.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/state/posted_message_v1/mod.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/state/posted_message_v1/unreliable.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/state/posted_vaa_v1.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/state/signature_set.rs",
    "target_chains/solana/programs/core-bridge/src/legacy/utils/mod.rs",
    "target_chains/solana/programs/core-bridge/src/lib.rs",
    "target_chains/solana/programs/core-bridge/src/processor/mod.rs",
    "target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_encoded_vaa.rs",
    "target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/close_signature_set.rs",
    "target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/init_encoded_vaa.rs",
    "target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/mod.rs",
    "target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/post_vaa_v1.rs",
    "target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/verify_encoded_vaa_v1.rs",
    "target_chains/solana/programs/core-bridge/src/processor/parse_and_verify_vaa/write_encoded_vaa.rs",
    "target_chains/solana/programs/core-bridge/src/processor/post_message/close_message_v1.rs",
    "target_chains/solana/programs/core-bridge/src/processor/post_message/finalize_message_v1.rs",
    "target_chains/solana/programs/core-bridge/src/processor/post_message/init_message_v1.rs",
    "target_chains/solana/programs/core-bridge/src/processor/post_message/mod.rs",
    "target_chains/solana/programs/core-bridge/src/processor/post_message/write_message_v1.rs",
    "target_chains/solana/programs/core-bridge/src/sdk/mod.rs",
    "target_chains/solana/programs/core-bridge/src/sdk/prepare_message.rs",
    "target_chains/solana/programs/core-bridge/src/sdk/publish_message.rs",
    "target_chains/solana/programs/core-bridge/src/state/encoded_vaa.rs",
    "target_chains/solana/programs/core-bridge/src/state/mod.rs",
    "target_chains/solana/programs/core-bridge/src/types.rs",
    "target_chains/solana/programs/core-bridge/src/utils/cpi.rs",
    "target_chains/solana/programs/core-bridge/src/utils/mod.rs",
    "target_chains/solana/programs/core-bridge/src/utils/vaa/mod.rs",
    "target_chains/solana/programs/core-bridge/src/utils/vaa/zero_copy/encoded_vaa.rs",
    "target_chains/solana/programs/core-bridge/src/utils/vaa/zero_copy/mod.rs",
    "target_chains/solana/programs/core-bridge/src/utils/vaa/zero_copy/posted_vaa_v1.rs",
    "target_chains/solana/programs/pyth-price-store/src/accounts/buffer.rs",
    "target_chains/solana/programs/pyth-price-store/src/accounts/config.rs",
    "target_chains/solana/programs/pyth-price-store/src/accounts/errors.rs",
    "target_chains/solana/programs/pyth-price-store/src/accounts/publisher_config.rs",
    "target_chains/solana/programs/pyth-price-store/src/accounts.rs",
    "target_chains/solana/programs/pyth-price-store/src/error.rs",
    "target_chains/solana/programs/pyth-price-store/src/instruction.rs",
    "target_chains/solana/programs/pyth-price-store/src/lib.rs",
    "target_chains/solana/programs/pyth-price-store/src/processor/initialize.rs",
    "target_chains/solana/programs/pyth-price-store/src/processor/initialize_publisher.rs",
    "target_chains/solana/programs/pyth-price-store/src/processor/submit_prices.rs",
    "target_chains/solana/programs/pyth-price-store/src/processor.rs",
    "target_chains/solana/programs/pyth-price-store/src/validate.rs",
    "target_chains/solana/programs/pyth-push-oracle/src/lib.rs",
    "target_chains/solana/programs/pyth-push-oracle/src/sdk.rs",
    "target_chains/solana/programs/pyth-solana-receiver/src/error.rs",
    "target_chains/solana/programs/pyth-solana-receiver/src/lib.rs",
    "target_chains/solana/programs/pyth-solana-receiver/src/sdk.rs",
    "target_chains/ethereum/contracts/contracts/pyth/Pyth.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythAccumulator.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythDeprecatedStructs.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythGetters.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythGovernance.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythGovernanceInstructions.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythInternalStructs.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythSetters.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythState.sol",
    "target_chains/ethereum/contracts/contracts/pyth/PythUpgradable.sol",
]
target_scopes = [
    # 1. Oracle / published-value correctness
    "Critical/High. Manipulation or incorrect publication of Pyth oracle prices or other published values, including arbitrary manipulation or on-chain program flaws causing inaccurate prices when >= 3/4 of contributing publishers are accurate",
    # 2. Ownership / governance control
    "Critical. Unauthorized ownership/control of Pyth mainnet contracts or governance voting result manipulation that changes execution away from the voted outcome",
    # 3. User / staked funds loss
    "Critical. Direct theft, loss, or locking of user funds or funds staked on Pyth, whether at-rest or in-motion, excluding unclaimed yield",
    # 4. Fund freezing / insolvency
    "Critical/High. Permanent or temporary freezing of funds, or protocol insolvency",
    # 5. Yield-specific loss
    "High. Theft or permanent freezing of unclaimed yield",
    # 6. PDA / permissionless service key exposure
    "High. Exposure of private keys controlled by the PDA or permissionless services",
    # 7. Availability / gas griefing
    "Medium. Smart contract unable to operate due to lack of token funds, block stuffing, griefing without profit motive, or theft of gas",
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit + fuzzing questions for one Pyth Network target.

    ```
    target_file format:
    "'File Name: target_chains/ethereum/contracts/contracts/pyth/Pyth.sol -> Scope: Critical Arbitrarily manipulate Pyth oracle prices'"
    """

    prompt = f"""
    ```
    
    Generate exploit-focused security audit and fuzzing questions for this exact Pyth Network target:
    
    {target_file}
    
    Use live context from the project if available: data sources, Wormhole emitters, VAA formats, governance state, fee config, staking state, Entropy config, Lazer channels, and known invariant assumptions.
    
    Protocol focus:
    Pyth publishes first-party oracle prices and other market data across Ethereum, Solana, Sui, EVM Lazer, Sui Lazer, Cardano Lazer, Entropy, staking, and governance contracts/programs.
    
    Core invariants:
    
    * Invalid price, accumulator, Lazer, Entropy, staking, or governance messages must never be accepted.
    * Signer, data source, emitter, chain, version, freshness, and replay checks must bind every update to intended Pyth state.
    * Oracle prices must not be manipulable when required publishers are accurate.
    * User funds, staked funds, unclaimed yield, and protocol funds must not be stolen, frozen, or lost.
    * Governance execution and ownership/upgrade paths must match the approved payload and intended authority.
    * Contracts/programs must remain operable and not allow scoped gas theft, block stuffing, griefing, or unbounded gas impact.
    
    Rules:
    
    * Treat `File Name:` as the exact file/module.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Attacker is unprivileged: an updater, relayer, transaction sender, price consumer, governance message submitter, staking user, Entropy user/provider, or Lazer updater.
    * Do not rely on admin compromise, malicious governance, leaked keys, third-party oracle lies, Sybil/51% attacks, phishing, or public-mainnet testing.
    * Generate 10 to 20 high-signal questions.
    * At least 70% must be multi-step flow, invariant, fuzz, accounting, state-transition, or cross-module questions.
    * Every question must be testable by PoC, unit test, fuzz test, invariant test, or differential test.
    * Avoid generic checklist questions and repeated root causes.
    * Note any question u must target valid issue u think could be possible 
    
    High-value attack surfaces:
    
    * Price update validation: `updatePriceFeeds`, `parsePriceFeedUpdates`, TWAP, freshness, fees, refunds, and stale checks.
    * Accumulator and Merkle logic: proof verification, message ordering, parser bounds, and malformed payload handling.
    * Wormhole/cross-chain receiver logic: VAA emitter, chain, guardian, sequence, replay, and governance payload checks.
    * Governance and upgrade paths: ownership, executor calls, target calls, claimed VAAs, and account/object authority.
    * Staking, yield, and accounting: locked funds, withdrawals, rewards, stake caps, fees, and token balances.
    * Entropy and Lazer: provider fulfillment, callback payment, randomness delivery, signer sets, channels, and feed updates.
    
    Impact mapping:
    
    * Oracle manipulation: Attacker causes accepted Pyth price or published value to differ from valid data.
    * Ownership takeover: Attacker gains owner, upgrade, or admin-equivalent control of mainnet contracts.
    * Fund loss/freeze: Attacker steals or freezes user funds, staked funds, protocol funds, or unclaimed yield.
    * Governance manipulation: Executed result deviates from the voted or approved outcome.
    * Insolvency: Accounting bug creates liabilities exceeding assets.
    * Gas/griefing: Attacker causes scoped token depletion, block stuffing, gas theft, or unbounded gas consumption.
    
    Each question must include:
    
    1. target function/module;
    2. attacker action;
    3. preconditions;
    4. call sequence;
    5. invariant tested;
    6. scoped impact;
    7. proof idea.
    
    Output only valid Python. No markdown. No explanations.
    
    questions = [
    "[File: {target_file}] [Function: symbol_or_module] Can an unprivileged ATTACKER_ACTION under PRECONDITIONS trigger CALL_SEQUENCE, violating INVARIANT, causing scoped impact: SCOPE_IMPACT? Proof idea: fuzz/state-test PARAMETERS and assert EXPECTED_PROPERTY.",
    ]
    """
    return prompt


def audit_format(question: str) -> str:
    """
    Generate a focused Pyth exploit-question validation prompt.
    """
    return f"""# QUESTION SCAN PROMPT

## Exploit Question
{question}

## Scope Rules
- Audit only production Pyth Network code.
- Do not ask for repo contents or claim files are missing.
- Ignore tests, docs, mocks, generated files, scripts, configs, build files, IDE files, and package metadata.

## Objective
Decide whether the question leads to a real, reachable Pyth vulnerability.
The attacker must be unprivileged and enter through price updates, transaction execution, VAA/governance relay, staking, Entropy, Lazer update, or a public contract path.
The impact must match the provided target scope.
Prefer #NoVulnerability unless the path is concrete, local-testable, and bounty-grade.

## Method
1. Trace the attacker-controlled entrypoint.
2. Map it to exact production Pyth files/functions.
3. Check the relevant Pyth guard: signer/data-source, emitter/chain, replay, freshness, Merkle/VAA, authority, fee/accounting, or parser bounds.
4. Decide whether the questioned invariant can actually break under intended deployment.
5. Prove root cause with file/function/line references.
6. Confirm realistic likelihood and exact scoped impact.
7. Reject if current validation already prevents the exploit.

## Reject Immediately
- Requires trusted role, leaked key, malicious governance majority, or privileged operator access.
- Requires third-party oracle lying, Sybil/51% attack, phishing, public-mainnet testing, or DDoS/brute force.
- Only affects tests, docs, configs, scripts, mocks, generated code, or local deployment choices.
- External dependency behavior is the only cause.
- Impact is only logging, observability, local misconfiguration, non-security correctness, harmless revert, stale read, rejected update, or theoretical risk.
- No concrete scoped impact or no realistic exploit path.

## Output
If valid:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If invalid, output exactly:
#NoVulnerability found for this question.
"""


def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for Pyth Network.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Access Rules (Strict)
- Treat production Pyth files in the provided scope as accessible context.
- Do not claim missing/inaccessible files.
- Do not ask for repository contents.
- Do not scan tests, docs, build files, IDE files, configs, generated files, resources, or package metadata as audited targets.

## Objective
Use the external report's vulnerability class as a hint to find valid issues based on Pyth Network Immunefi scope.
Focus on externally reachable Pyth issues triggered by an unprivileged updater, transaction sender, governance message submitter, staking user, Entropy user/provider, Lazer updater, or relayer.
Only report an analog if Pyth code has its own reachable root cause and the impact matches the provided target scope.

## Method
1. Classify vuln type: price validation, accumulator/Merkle proof, VAA parsing, replay, governance execution, upgrade ownership, staking accounting, Entropy fulfillment, Lazer signer/channel state, parser bounds, fee/refund accounting, or receiver mismatch.
2. Map to Pyth components and exact production files.
3. Prove root cause with exact file/function/module/line references.
4. Confirm concrete Pyth scoped impact and realistic likelihood.
5. Explain the attacker-controlled entry path and why Pyth code is a necessary vulnerable step.
6. Reject if the impact does not match the provided target scope.

## Disqualify Immediately
- No reachable attacker-controlled entry path.
- Requires trusted role, leaked key, malicious governance majority, or privileged operator access.
- Requires third-party oracle lying, Sybil/51% attack, phishing, public-mainnet testing, or DDoS/brute force.
- External dependency behavior is the only cause.
- Test/docs/config/build-only issue.
- Theoretical-only issue with no protocol impact.
- Impact is only local misconfiguration, observability noise, logging noise, harmless revert, stale read, or non-security correctness.
- Impact or likelihood missing.

## Output (Strict)
If valid analog exists, output:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

If not, output exactly:
#NoVulnerability found for this question.

No extra text.
"""
    return prompt
