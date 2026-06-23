import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 20
# todo: the path from https:///github.com/dfinity/ICRC-1
SOURCE_REPO = "nervosnetwork/ckb"
# todo: the name of the repository
REPO_NAME = "ckb"
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
    'block-filter/src/filter.rs',
    'block-filter/src/lib.rs',
    'chain/src/chain_controller.rs',
    'chain/src/chain_service.rs',
    'chain/src/init.rs',
    'chain/src/init_load_unverified.rs',
    'chain/src/lib.rs',
    'chain/src/orphan_broker.rs',
    'chain/src/preload_unverified_blocks_channel.rs',
    'chain/src/utils/forkchanges.rs',
    'chain/src/utils/mod.rs',
    'chain/src/utils/orphan_block_pool.rs',
    'chain/src/verify.rs',
    'ckb-bin/src/cli.rs',
    'ckb-bin/src/helper.rs',
    'ckb-bin/src/lib.rs',
    'ckb-bin/src/setup.rs',
    'ckb-bin/src/setup_guard.rs',
    'ckb-bin/src/subcommand/daemon.rs',
    'ckb-bin/src/subcommand/export.rs',
    'ckb-bin/src/subcommand/import.rs',
    'ckb-bin/src/subcommand/init.rs',
    'ckb-bin/src/subcommand/list_hashes.rs',
    'ckb-bin/src/subcommand/migrate.rs',
    'ckb-bin/src/subcommand/miner.rs',
    'ckb-bin/src/subcommand/mod.rs',
    'ckb-bin/src/subcommand/peer_id.rs',
    'ckb-bin/src/subcommand/replay.rs',
    'ckb-bin/src/subcommand/reset_data.rs',
    'ckb-bin/src/subcommand/run.rs',
    'ckb-bin/src/subcommand/stats.rs',
    'db-migration/src/lib.rs',
    'db-schema/src/lib.rs',
    'db/src/db.rs',
    'db/src/db_with_ttl.rs',
    'db/src/iter.rs',
    'db/src/lib.rs',
    'db/src/read_only_db.rs',
    'db/src/snapshot.rs',
    'db/src/transaction.rs',
    'db/src/write_batch.rs',
    'error/src/convert.rs',
    'error/src/internal.rs',
    'error/src/lib.rs',
    'error/src/prelude.rs',
    'error/src/util.rs',
    'freezer/src/freezer.rs',
    'freezer/src/freezer_files.rs',
    'freezer/src/lib.rs',
    'miner/src/client.rs',
    'miner/src/lib.rs',
    'miner/src/miner.rs',
    'miner/src/worker/dummy.rs',
    'miner/src/worker/eaglesong_simple.rs',
    'miner/src/worker/mod.rs',
    'network/src/behaviour.rs',
    'network/src/compress.rs',
    'network/src/errors.rs',
    'network/src/lib.rs',
    'network/src/network.rs',
    'network/src/network_group.rs',
    'network/src/peer.rs',
    'network/src/peer_registry.rs',
    'network/src/peer_store/addr_manager.rs',
    'network/src/peer_store/anchors.rs',
    'network/src/peer_store/ban_list.rs',
    'network/src/peer_store/browser.rs',
    'network/src/peer_store/mod.rs',
    'network/src/peer_store/peer_store_db.rs',
    'network/src/peer_store/peer_store_impl.rs',
    'network/src/peer_store/types.rs',
    'network/src/protocols/disconnect_message.rs',
    'network/src/protocols/discovery/addr.rs',
    'network/src/protocols/discovery/mod.rs',
    'network/src/protocols/discovery/protocol.rs',
    'network/src/protocols/discovery/state.rs',
    'network/src/protocols/feeler.rs',
    'network/src/protocols/hole_punching/component/connection_request.rs',
    'network/src/protocols/hole_punching/component/connection_request_delivered.rs',
    'network/src/protocols/hole_punching/component/connection_sync.rs',
    'network/src/protocols/hole_punching/component/mod.rs',
    'network/src/protocols/hole_punching/mod.rs',
    'network/src/protocols/hole_punching/status.rs',
    'network/src/protocols/identify/mod.rs',
    'network/src/protocols/identify/protocol.rs',
    'network/src/protocols/mod.rs',
    'network/src/protocols/ping.rs',
    'network/src/protocols/support_protocols.rs',
    'network/src/proxy.rs',
    'network/src/services/dns_seeding/mod.rs',
    'network/src/services/dns_seeding/seed_record.rs',
    'network/src/services/dump_peer_store.rs',
    'network/src/services/mod.rs',
    'network/src/services/outbound_peer.rs',
    'network/src/services/protocol_type_checker.rs',
    'notify/src/lib.rs',
    'pow/src/dummy.rs',
    'pow/src/eaglesong.rs',
    'pow/src/eaglesong_blake2b.rs',
    'pow/src/lib.rs',
    'resource/specs/mainnet.toml',
    'resource/specs/testnet.toml',
    'resource/src/lib.rs',
    'resource/src/template.rs',
    'rpc/src/error.rs',
    'rpc/src/lib.rs',
    'rpc/src/module/alert.rs',
    'rpc/src/module/chain.rs',
    'rpc/src/module/debug.rs',
    'rpc/src/module/experiment.rs',
    'rpc/src/module/indexer.rs',
    'rpc/src/module/miner.rs',
    'rpc/src/module/mod.rs',
    'rpc/src/module/net.rs',
    'rpc/src/module/pool.rs',
    'rpc/src/module/rich_indexer.rs',
    'rpc/src/module/stats.rs',
    'rpc/src/module/subscription.rs',
    'rpc/src/module/terminal.rs',
    'rpc/src/server.rs',
    'rpc/src/service_builder.rs',
    'rpc/src/util/fee_rate.rs',
    'rpc/src/util/mod.rs',
    'script/src/cost_model.rs',
    'script/src/error.rs',
    'script/src/lib.rs',
    'script/src/scheduler.rs',
    'script/src/syscalls/close.rs',
    'script/src/syscalls/current_cycles.rs',
    'script/src/syscalls/debugger.rs',
    'script/src/syscalls/exec.rs',
    'script/src/syscalls/exec_v2.rs',
    'script/src/syscalls/generator.rs',
    'script/src/syscalls/inherited_fd.rs',
    'script/src/syscalls/load_block_extension.rs',
    'script/src/syscalls/load_cell.rs',
    'script/src/syscalls/load_cell_data.rs',
    'script/src/syscalls/load_header.rs',
    'script/src/syscalls/load_input.rs',
    'script/src/syscalls/load_script.rs',
    'script/src/syscalls/load_script_hash.rs',
    'script/src/syscalls/load_tx.rs',
    'script/src/syscalls/load_witness.rs',
    'script/src/syscalls/mod.rs',
    'script/src/syscalls/pause.rs',
    'script/src/syscalls/pipe.rs',
    'script/src/syscalls/process_id.rs',
    'script/src/syscalls/read.rs',
    'script/src/syscalls/spawn.rs',
    'script/src/syscalls/utils.rs',
    'script/src/syscalls/vm_version.rs',
    'script/src/syscalls/wait.rs',
    'script/src/syscalls/write.rs',
    'script/src/type_id.rs',
    'script/src/types.rs',
    'script/src/verify.rs',
    'script/src/verify_env.rs',
    'shared/src/block_status.rs',
    'shared/src/chain_services_builder.rs',
    'shared/src/lib.rs',
    'shared/src/shared.rs',
    'shared/src/shared_builder.rs',
    'shared/src/types/header_map/backend.rs',
    'shared/src/types/header_map/backend_sled.rs',
    'shared/src/types/header_map/kernel_lru.rs',
    'shared/src/types/header_map/memory.rs',
    'shared/src/types/header_map/mod.rs',
    'shared/src/types/mod.rs',
    'spec/src/consensus.rs',
    'spec/src/error.rs',
    'spec/src/hardfork.rs',
    'spec/src/lib.rs',
    'spec/src/versionbits/convert.rs',
    'spec/src/versionbits/mod.rs',
    'src/main.rs',
    'store/src/cache.rs',
    'store/src/cell.rs',
    'store/src/data_loader_wrapper.rs',
    'store/src/db.rs',
    'store/src/lib.rs',
    'store/src/snapshot.rs',
    'store/src/store.rs',
    'store/src/transaction.rs',
    'store/src/write_batch.rs',
    'sync/src/filter/get_block_filter_check_points_process.rs',
    'sync/src/filter/get_block_filter_hashes_process.rs',
    'sync/src/filter/get_block_filters_process.rs',
    'sync/src/filter/mod.rs',
    'sync/src/lib.rs',
    'sync/src/net_time_checker.rs',
    'sync/src/relayer/block_proposal_process.rs',
    'sync/src/relayer/block_transactions_process.rs',
    'sync/src/relayer/block_transactions_verifier.rs',
    'sync/src/relayer/block_uncles_verifier.rs',
    'sync/src/relayer/compact_block_process.rs',
    'sync/src/relayer/compact_block_verifier.rs',
    'sync/src/relayer/get_block_proposal_process.rs',
    'sync/src/relayer/get_block_transactions_process.rs',
    'sync/src/relayer/get_transactions_process.rs',
    'sync/src/relayer/mod.rs',
    'sync/src/relayer/transaction_hashes_process.rs',
    'sync/src/relayer/transactions_process.rs',
    'sync/src/status.rs',
    'sync/src/synchronizer/block_fetcher.rs',
    'sync/src/synchronizer/block_process.rs',
    'sync/src/synchronizer/get_blocks_process.rs',
    'sync/src/synchronizer/get_headers_process.rs',
    'sync/src/synchronizer/headers_process.rs',
    'sync/src/synchronizer/in_ibd_process.rs',
    'sync/src/synchronizer/mod.rs',
    'sync/src/types/mod.rs',
    'sync/src/utils.rs',
    'traits/src/cell_data_provider.rs',
    'traits/src/epoch_provider.rs',
    'traits/src/extension_provider.rs',
    'traits/src/header_provider.rs',
    'traits/src/lib.rs',
    'tx-pool/src/block_assembler/candidate_uncles.rs',
    'tx-pool/src/block_assembler/mod.rs',
    'tx-pool/src/block_assembler/process.rs',
    'tx-pool/src/callback.rs',
    'tx-pool/src/component/edges.rs',
    'tx-pool/src/component/entry.rs',
    'tx-pool/src/component/links.rs',
    'tx-pool/src/component/mod.rs',
    'tx-pool/src/component/orphan.rs',
    'tx-pool/src/component/pool_map.rs',
    'tx-pool/src/component/recent_reject.rs',
    'tx-pool/src/component/sort_key.rs',
    'tx-pool/src/component/tx_selector.rs',
    'tx-pool/src/component/verify_queue.rs',
    'tx-pool/src/error.rs',
    'tx-pool/src/lib.rs',
    'tx-pool/src/persisted.rs',
    'tx-pool/src/pool.rs',
    'tx-pool/src/pool_cell.rs',
    'tx-pool/src/process.rs',
    'tx-pool/src/service.rs',
    'tx-pool/src/util.rs',
    'tx-pool/src/verify_mgr.rs',
    'util/app-config/src/app_config.rs',
    'util/app-config/src/args.rs',
    'util/app-config/src/cli.rs',
    'util/app-config/src/configs/db.rs',
    'util/app-config/src/configs/fee_estimator.rs',
    'util/app-config/src/configs/indexer.rs',
    'util/app-config/src/configs/memory_tracker.rs',
    'util/app-config/src/configs/miner.rs',
    'util/app-config/src/configs/mod.rs',
    'util/app-config/src/configs/network.rs',
    'util/app-config/src/configs/network_alert.rs',
    'util/app-config/src/configs/notify.rs',
    'util/app-config/src/configs/rich_indexer.rs',
    'util/app-config/src/configs/rpc.rs',
    'util/app-config/src/configs/store.rs',
    'util/app-config/src/configs/tx_pool.rs',
    'util/app-config/src/exit_code.rs',
    'util/app-config/src/legacy/mod.rs',
    'util/app-config/src/legacy/store.rs',
    'util/app-config/src/legacy/tx_pool.rs',
    'util/app-config/src/lib.rs',
    'util/app-config/src/sentry_config.rs',
    'util/build-info/src/lib.rs',
    'util/chain-iter/src/lib.rs',
    'util/channel/src/lib.rs',
    'util/constant/src/consensus.rs',
    'util/constant/src/default_assume_valid_target.rs',
    'util/constant/src/hardfork/mainnet.rs',
    'util/constant/src/hardfork/mod.rs',
    'util/constant/src/hardfork/testnet.rs',
    'util/constant/src/latest_assume_valid_target.rs',
    'util/constant/src/lib.rs',
    'util/constant/src/softfork/mainnet.rs',
    'util/constant/src/softfork/mod.rs',
    'util/constant/src/softfork/testnet.rs',
    'util/constant/src/store.rs',
    'util/constant/src/sync.rs',
    'util/crypto/src/lib.rs',
    'util/crypto/src/secp/error.rs',
    'util/crypto/src/secp/generator.rs',
    'util/crypto/src/secp/mod.rs',
    'util/crypto/src/secp/privkey.rs',
    'util/crypto/src/secp/pubkey.rs',
    'util/crypto/src/secp/signature.rs',
    'util/dao/src/lib.rs',
    'util/dao/utils/src/error.rs',
    'util/dao/utils/src/lib.rs',
    'util/fee-estimator/src/constants.rs',
    'util/fee-estimator/src/error.rs',
    'util/fee-estimator/src/estimator/confirmation_fraction.rs',
    'util/fee-estimator/src/estimator/mod.rs',
    'util/fee-estimator/src/estimator/weight_units_flow.rs',
    'util/fee-estimator/src/lib.rs',
    'util/fixed-hash/core/src/error.rs',
    'util/fixed-hash/core/src/impls.rs',
    'util/fixed-hash/core/src/lib.rs',
    'util/fixed-hash/core/src/serde.rs',
    'util/fixed-hash/core/src/std_cmp.rs',
    'util/fixed-hash/core/src/std_convert.rs',
    'util/fixed-hash/core/src/std_default.rs',
    'util/fixed-hash/core/src/std_fmt.rs',
    'util/fixed-hash/core/src/std_hash.rs',
    'util/fixed-hash/core/src/std_str.rs',
    'util/fixed-hash/macros/src/lib.rs',
    'util/fixed-hash/src/lib.rs',
    'util/gen-types/src/conversion/blockchain/mod.rs',
    'util/gen-types/src/conversion/blockchain/std_env.rs',
    'util/gen-types/src/conversion/mod.rs',
    'util/gen-types/src/conversion/network.rs',
    'util/gen-types/src/conversion/primitive.rs',
    'util/gen-types/src/conversion/utilities.rs',
    'util/gen-types/src/core.rs',
    'util/gen-types/src/extension/calc_hash.rs',
    'util/gen-types/src/extension/capacity.rs',
    'util/gen-types/src/extension/check_data.rs',
    'util/gen-types/src/extension/mod.rs',
    'util/gen-types/src/extension/rust_core_traits.rs',
    'util/gen-types/src/extension/serialized_size.rs',
    'util/gen-types/src/extension/shortcut.rs',
    'util/gen-types/src/lib.rs',
    'util/gen-types/src/prelude.rs',
    'util/hash/src/lib.rs',
    'util/indexer-sync/src/custom_filters.rs',
    'util/indexer-sync/src/error.rs',
    'util/indexer-sync/src/lib.rs',
    'util/indexer-sync/src/pool.rs',
    'util/indexer-sync/src/store.rs',
    'util/indexer/src/indexer.rs',
    'util/indexer/src/lib.rs',
    'util/indexer/src/service.rs',
    'util/indexer/src/store/mod.rs',
    'util/indexer/src/store/rocksdb.rs',
    'util/instrument/src/export.rs',
    'util/instrument/src/import.rs',
    'util/instrument/src/lib.rs',
    'util/jsonrpc-types/src/alert.rs',
    'util/jsonrpc-types/src/block_template.rs',
    'util/jsonrpc-types/src/blockchain.rs',
    'util/jsonrpc-types/src/bytes.rs',
    'util/jsonrpc-types/src/cell.rs',
    'util/jsonrpc-types/src/debug.rs',
    'util/jsonrpc-types/src/experiment.rs',
    'util/jsonrpc-types/src/fee_estimator.rs',
    'util/jsonrpc-types/src/fee_rate.rs',
    'util/jsonrpc-types/src/fixed_bytes.rs',
    'util/jsonrpc-types/src/indexer.rs',
    'util/jsonrpc-types/src/info.rs',
    'util/jsonrpc-types/src/json_schema.rs',
    'util/jsonrpc-types/src/lib.rs',
    'util/jsonrpc-types/src/net.rs',
    'util/jsonrpc-types/src/pool.rs',
    'util/jsonrpc-types/src/primitive.rs',
    'util/jsonrpc-types/src/proposal_short_id.rs',
    'util/jsonrpc-types/src/subscription.rs',
    'util/jsonrpc-types/src/terminal.rs',
    'util/jsonrpc-types/src/uints.rs',
    'util/launcher/src/lib.rs',
    'util/light-client-protocol-server/src/components/get_blocks_proof.rs',
    'util/light-client-protocol-server/src/components/get_last_state.rs',
    'util/light-client-protocol-server/src/components/get_last_state_proof.rs',
    'util/light-client-protocol-server/src/components/get_transactions_proof.rs',
    'util/light-client-protocol-server/src/components/mod.rs',
    'util/light-client-protocol-server/src/constant.rs',
    'util/light-client-protocol-server/src/lib.rs',
    'util/light-client-protocol-server/src/prelude.rs',
    'util/light-client-protocol-server/src/status.rs',
    'util/logger-config/src/lib.rs',
    'util/logger-service/src/lib.rs',
    'util/logger/src/lib.rs',
    'util/memory-tracker/src/jemalloc.rs',
    'util/memory-tracker/src/lib.rs',
    'util/memory-tracker/src/process.rs',
    'util/memory-tracker/src/rocksdb.rs',
    'util/metrics-config/src/lib.rs',
    'util/metrics-service/src/lib.rs',
    'util/metrics/src/lib.rs',
    'util/migrate/src/lib.rs',
    'util/migrate/src/migrate.rs',
    'util/migrate/src/migrations/add_block_extension_cf.rs',
    'util/migrate/src/migrations/add_block_filter.rs',
    'util/migrate/src/migrations/add_block_filter_hash.rs',
    'util/migrate/src/migrations/add_chain_root_mmr.rs',
    'util/migrate/src/migrations/add_extra_data_hash.rs',
    'util/migrate/src/migrations/add_number_hash_mapping.rs',
    'util/migrate/src/migrations/cell.rs',
    'util/migrate/src/migrations/mod.rs',
    'util/migrate/src/migrations/set_2019_block_cycle_zero.rs',
    'util/migrate/src/migrations/table_to_struct.rs',
    'util/multisig/src/error.rs',
    'util/multisig/src/lib.rs',
    'util/multisig/src/secp256k1.rs',
    'util/network-alert/src/alert_relayer.rs',
    'util/network-alert/src/lib.rs',
    'util/network-alert/src/notifier.rs',
    'util/network-alert/src/verifier.rs',
    'util/occupied-capacity/core/src/lib.rs',
    'util/occupied-capacity/core/src/units.rs',
    'util/occupied-capacity/macros/src/lib.rs',
    'util/occupied-capacity/src/lib.rs',
    'util/onion/src/lib.rs',
    'util/onion/src/onion_service.rs',
    'util/onion/src/tor_controller.rs',
    'util/proposal-table/src/lib.rs',
    'util/rational/src/lib.rs',
    'util/reward-calculator/src/lib.rs',
    'util/rich-indexer/src/indexer/insert.rs',
    'util/rich-indexer/src/indexer/mod.rs',
    'util/rich-indexer/src/indexer/remove.rs',
    'util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs',
    'util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs',
    'util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs',
    'util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs',
    'util/rich-indexer/src/indexer_handle/mod.rs',
    'util/rich-indexer/src/lib.rs',
    'util/rich-indexer/src/service.rs',
    'util/rich-indexer/src/store.rs',
    'util/runtime/src/browser.rs',
    'util/runtime/src/lib.rs',
    'util/runtime/src/native.rs',
    'util/snapshot/src/lib.rs',
    'util/spawn/src/lib.rs',
    'util/src/lib.rs',
    'util/src/linked_hash_set.rs',
    'util/src/shrink_to_fit.rs',
    'util/src/strings.rs',
    'util/stop-handler/src/lib.rs',
    'util/stop-handler/src/stop_register.rs',
    'util/systemtime/src/lib.rs',
    'util/types/src/block_number_and_hash.rs',
    'util/types/src/constants.rs',
    'util/types/src/conversion/blockchain.rs',
    'util/types/src/conversion/mod.rs',
    'util/types/src/conversion/storage.rs',
    'util/types/src/conversion/utilities.rs',
    'util/types/src/core/advanced_builders.rs',
    'util/types/src/core/blockchain.rs',
    'util/types/src/core/cell.rs',
    'util/types/src/core/error.rs',
    'util/types/src/core/extras.rs',
    'util/types/src/core/fee_estimator.rs',
    'util/types/src/core/fee_rate.rs',
    'util/types/src/core/hardfork/ckb2021.rs',
    'util/types/src/core/hardfork/ckb2023.rs',
    'util/types/src/core/hardfork/helper.rs',
    'util/types/src/core/hardfork/mod.rs',
    'util/types/src/core/mod.rs',
    'util/types/src/core/reward.rs',
    'util/types/src/core/transaction_meta.rs',
    'util/types/src/core/tx_pool.rs',
    'util/types/src/core/views.rs',
    'util/types/src/extension.rs',
    'util/types/src/global.rs',
    'util/types/src/lib.rs',
    'util/types/src/prelude.rs',
    'util/types/src/utilities/block_filter.rs',
    'util/types/src/utilities/difficulty.rs',
    'util/types/src/utilities/merkle_mountain_range.rs',
    'util/types/src/utilities/merkle_tree.rs',
    'util/types/src/utilities/mod.rs',
    'verification/contextual/src/contextual_block_verifier.rs',
    'verification/contextual/src/lib.rs',
    'verification/contextual/src/uncles_verifier.rs',
    'verification/src/block_verifier.rs',
    'verification/src/cache.rs',
    'verification/src/convert.rs',
    'verification/src/error.rs',
    'verification/src/genesis_verifier.rs',
    'verification/src/header_verifier.rs',
    'verification/src/lib.rs',
    'verification/src/transaction_verifier.rs',
    'verification/traits/src/lib.rs',
]

target_scopes = [
    # Critical: 15001 - 25000 points
    "Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network",
    "Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation",
    "Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy",
    # High: 10001 - 15000 points
    "High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node",
    "High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs",
    "High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts",
    # Medium: 2001 - 10000 points
    "Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism",
    # Low: 501 - 2000 points
    "Low (501 - 2000 points). Any other important performance improvements for CKB",
    # Note: 0 - 500 points
    "Note (0 - 500 points). Any local RPC API crash",
    "Note (0 - 500 points). Any local command line crash",
]


def question_generator(target_file: str) -> str:
    """
    Generate exploit-focused audit + fuzzing questions for one Nervos CKB target.

    ```
    target_file format:
    "'File Name: verification/src/block_verifier.rs -> Scope: Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation'"
    """

    prompt = f"""
    ```
    
    Generate exploit-focused security audit and fuzzing questions for this exact Nervos CKB target:
    
    {target_file}
    
    Use live context from the project if available: consensus specs, hardfork/versionbits state, header/block/transaction verification, CKB-VM/syscalls, tx-pool policy, sync state, P2P message formats, fork choice, orphan handling, DAO/reward/fee accounting, database snapshots, RPC/indexer behavior, and known invariant assumptions.
    
    Protocol focus:
    Nervos CKB is a public permissionless proof-of-work layer-1 blockchain. This codebase validates consensus, blocks, headers, transactions, cell state, CKB-VM script execution, peer-to-peer networking, synchronization, tx-pool admission, storage, miner block assembly, RPC, and operator-facing node behavior.
    
    Core invariants:
    
    * Invalid blocks, headers, uncles, transactions, scripts, P2P messages, or persisted state must never be accepted as valid consensus or security-relevant state.
    * PoW target, epoch, timestamp, versionbits, hardfork, proposal, uncle, block extension, and fork-choice checks must be deterministic across honest nodes.
    * Transaction authorization, since/maturity, capacity conservation, occupied-capacity, DAO, reward, fee, and cell-spend rules must never be bypassed.
    * CKB-VM/syscall behavior, cycle accounting, script-visible data, VM version gates, and system script integration must match consensus expectations.
    * Peer, sync, tx-pool, RPC, indexer, database, snapshot, restart, and reorg state machines must remain bounded, crash-resistant, and consistent.
    * Attackers must not be able to cause whole-network crash, consensus deviation, CKB economy damage, CKB node crash, low-cost network congestion, incorrect CKB-VM/system script behavior, state storage weakness, important performance degradation, local RPC API crash, or local command line crash.
    
    Rules:
    
    * Treat `File Name:` as the exact file/module.
    * Treat `Scope:` as the ONLY impact to target.
    * Assume full repo context is accessible.
    * Do not ask for code or say anything is missing.
    * Attacker is unprivileged: a remote peer, block/header/transaction relayer, transaction sender, script author, miner with valid PoW on private/local chains, RPC caller, sync peer, tx-pool submitter, or operator-local user of supported CLI/RPC surfaces.
    * Do not rely on admin compromise, malicious maintainer/operator, leaked keys, malicious majority hashpower, Sybil/51% attacks, phishing, social engineering, public-mainnet testing, or unsupported local misconfiguration.
    * Generate 10 to 20 high-signal questions.
    * At least 70% must be multi-step flow, invariant, fuzz, accounting, state-transition, or cross-module questions.
    * Every question must be testable by PoC, unit test, fuzz test, invariant test, or differential test.
    * Avoid generic checklist questions and repeated root causes.
    * Note any question u must target valid issue u think could be possible 
    
    High-value attack surfaces:
    
    * Consensus validation: header, block, uncle, proposal, PoW, timestamp, epoch, versionbits, block extension, and fork-choice checks.
    * Transaction state transition: inputs, outputs, deps, witnesses, since locks, maturity, capacity, occupied capacity, DAO, rewards, fees, and cell status.
    * CKB-VM and scripts: syscall argument bounds, spawn/exec, load_cell/load_header/load_tx/load_witness, VM version gates, cycle limits, and script-visible data.
    * Network and sync: compressed frames, discovery, identify, ping, relay, headers, blocks, compact blocks, orphan handling, peer scoring, and message limits.
    * Tx-pool and miner assembly: admission/pre-verification divergence, orphan transactions, recent rejects, proposal selection, block template construction, and uncle/proposal eligibility.
    * Storage, database, RPC, and indexers: snapshots, write batches, migrations, restart/reorg consistency, RPC parsing, local crash paths, and stale security-relevant helper state.
    
    Impact mapping:
    
    * Critical 15001 - 25000: Attacker easily crashes the whole CKB network, causes consensus deviation, or damages the CKB economy.
    * High 10001 - 15000: Attacker easily crashes a CKB node, causes CKB network congestion with few costs, or triggers incorrect CKB-VM/system script behavior.
    * Medium 2001 - 10000: Attacker demonstrates a suboptimal CKB state storage mechanism with security-relevant or operational impact.
    * Low 501 - 2000: Attacker demonstrates an important CKB performance improvement opportunity.
    * Note 0 - 500: Attacker causes a local RPC API crash or local command line crash.
    
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
    Generate a focused Nervos CKB exploit-question validation prompt.
    """
    return f"""# QUESTION SCAN PROMPT

## Exploit Question
{question}

## Scope Rules
- Audit only production Nervos CKB code.
- Do not ask for repo contents or claim files are missing.
- Ignore tests, docs, mocks, generated files, scripts, configs, build files, IDE files, and package metadata.

## Objective
Decide whether the question leads to a real, reachable Nervos CKB vulnerability.
The attacker must be unprivileged and enter through P2P messages, sync/block/header relay, transaction submission, CKB-VM script execution, tx-pool admission, miner/block-template paths, RPC/CLI inputs, database restart/reorg state, or another supported production node path.
The impact must match the provided target scope.
Prefer #NoVulnerability unless the path is concrete, local-testable, and bounty-grade.

## Method
1. Trace the attacker-controlled entrypoint.
2. Map it to exact production CKB files/functions.
3. Check the relevant CKB guard: consensus validity, PoW, epoch, timestamp, hardfork/versionbits, transaction authorization, capacity/accounting, VM/syscall bounds, peer/sync limits, tx-pool policy, storage consistency, RPC validation, or parser bounds.
4. Decide whether the questioned invariant can actually break under intended deployment.
5. Prove root cause with file/function/line references.
6. Confirm realistic likelihood and exact scoped impact.
7. Reject if current validation already prevents the exploit.

## Reject Immediately
- Requires trusted role, leaked key, malicious maintainer/operator, privileged operator access, or unsupported local configuration.
- Requires malicious majority hashpower, Sybil/51% attack, phishing, social engineering, public-mainnet testing, or DDoS/brute force.
- Only affects tests, docs, configs, scripts, mocks, generated code, or local deployment choices.
- External dependency behavior is the only cause.
- Impact is only logging, observability, local misconfiguration, non-security correctness, harmless rejection, stale read with no security impact, ordinary peer disconnect, or theoretical risk.
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
    Generate a short cross-project analog scan prompt for Nervos CKB.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Access Rules (Strict)
- Treat production Nervos CKB files in the provided scope as accessible context.
- Do not claim missing/inaccessible files.
- Do not ask for repository contents.
- Do not scan tests, docs, build files, IDE files, configs, generated files, resources, or package metadata as audited targets.

## Objective
Use the external report's vulnerability class as a hint to find valid issues based on Nervos CKB bounty scope.
Focus on externally reachable CKB issues triggered by an unprivileged peer, transaction sender, block/header relayer, script author, sync peer, tx-pool submitter, RPC caller, miner/block-template caller, or supported local CLI/RPC user.
Only report an analog if CKB code has its own reachable root cause and the impact matches the provided target scope.

## Method
1. Classify vuln type: consensus validation, block/header parsing, transaction authorization, cell/capacity accounting, CKB-VM/syscall behavior, P2P compression/message parsing, sync state machine, tx-pool admission, fork choice/reorg, database persistence, RPC parsing, miner assembly, parser bounds, or resource accounting.
2. Map to CKB components and exact production files.
3. Prove root cause with exact file/function/module/line references.
4. Confirm concrete CKB scoped impact and realistic likelihood.
5. Explain the attacker-controlled entry path and why CKB code is a necessary vulnerable step.
6. Reject if the impact does not match the provided target scope.

## Disqualify Immediately
- No reachable attacker-controlled entry path.
- Requires trusted role, leaked key, malicious maintainer/operator, privileged operator access, unsupported local configuration, or malicious majority hashpower.
- Requires Sybil/51% attack, phishing, social engineering, public-mainnet testing, or DDoS/brute force.
- External dependency behavior is the only cause.
- Test/docs/config/build-only issue.
- Theoretical-only issue with no protocol impact.
- Impact is only local misconfiguration, observability noise, logging noise, harmless rejection, ordinary peer disconnect, stale read with no security impact, or non-security correctness.
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


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}

## Rules
- Validate only the submitted claim.
- Check Security.md/Researcher.md for scope, exclusions, and valid impact classes.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher impact.
- Reject admin-only, owner-only, trusted-operator, leaked-key, best-practice, docs/style, gas-only, and purely theoretical issues.
- Reject if the exploit requires unrealistic assumptions, victim mistakes, missing external context, or unsupported protocol behavior.
- A valid report must be triggerable by an unprivileged user, unless the claim proves privilege escalation from a user path.
- The final impact must match an in-scope bounty impact, not just a generic code bug.
- Reject any issue whose final impact is not one of the allowed CKB bounty impacts listed below.
- Prefer #NoVulnerability over speculative reports.

## Allowed Impact Scope
Only these impacts are valid:
- Critical (15001 - 25000 points). Vulnerabilities which could easily crash the whole CKB network.
- Critical (15001 - 25000 points). Vulnerabilities which could easily cause consensus deviation.
- Critical (15001 - 25000 points). Vulnerabilities which could easily damage CKB economy.
- High (10001 - 15000 points). Vulnerabilities which could easily crash a CKB node.
- High (10001 - 15000 points). Vulnerabilities or bad designs which could cause CKB network congestion with few costs.
- High (10001 - 15000 points). Incorrect implementation or behavior of CKB-VM or system scripts.
- Medium (2001 - 10000 points). Suboptimal implementation of CKB state storage mechanism.
- Low (501 - 2000 points). Any other important performance improvements for CKB.
- Note (0 - 500 points). Any local RPC API crash.
- Note (0 - 500 points). Any local command line crash.

If the submitted claim does not concretely prove one of the allowed impacts above, it is invalid.

## Required Validation Checks
All must pass:
1. Exact in-scope file, function, and line/code references.
2. Clear root cause and broken security/accounting assumption.
3. Reachable exploit path: preconditions -> attacker action -> trigger -> bad result.
4. Existing checks/guards reviewed and shown insufficient.
5. Concrete impact that exactly matches one allowed CKB bounty impact above, with realistic likelihood.
6. Reproducible proof path: unit PoC, fork test, invariant/fuzz test, or exact manual steps.
7. No obvious rejection reason from Security.md, known issues, privileges, or scope exclusions.

## Silent Triage Questions
Before output, internally answer:
- Can a normal external user trigger this?
- Does the code actually behave as claimed?
- Is the impact caused by this protocol, not by an external dependency alone?
- Is the loss/freeze/insolvency concrete, not hypothetical?
- Would a bounty triager accept the proof?
- What exact test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete allowed CKB bounty impact and severity rationale]

## Likelihood Explanation
[Attacker capability, required conditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or fuzz/invariant/fork test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt
