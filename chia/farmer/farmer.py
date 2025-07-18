from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
import traceback
from collections.abc import AsyncIterator
from dataclasses import dataclass
from math import floor
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional, Union, cast

import aiohttp
from chia_rs import AugSchemeMPL, ConsensusConstants, G1Element, G2Element, PrivateKey, ProofOfSpace
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint8, uint16, uint32, uint64

from chia.daemon.keychain_proxy import KeychainProxy, connect_to_keychain_and_validate, wrap_local_keychain
from chia.plot_sync.delta import Delta
from chia.plot_sync.receiver import Receiver
from chia.pools.pool_config import PoolWalletConfig, load_pool_config, update_pool_url
from chia.protocols import farmer_protocol, harvester_protocol
from chia.protocols.outbound_message import NodeType, make_msg
from chia.protocols.pool_protocol import (
    AuthenticationPayload,
    ErrorResponse,
    GetFarmerResponse,
    PoolErrorCode,
    PostFarmerPayload,
    PostFarmerRequest,
    PutFarmerPayload,
    PutFarmerRequest,
    get_current_authentication_token,
)
from chia.protocols.protocol_message_types import ProtocolMessageTypes
from chia.rpc.rpc_server import StateChangedProtocol, default_get_connections
from chia.server.server import ChiaServer, ssl_context_for_root
from chia.server.ws_connection import WSChiaConnection
from chia.ssl.create_ssl import get_mozilla_ca_crt
from chia.util.bech32m import decode_puzzle_hash, encode_puzzle_hash
from chia.util.byte_types import hexstr_to_bytes
from chia.util.config import config_path_for_filename, load_config, lock_and_load_config, save_config
from chia.util.errors import KeychainProxyConnectionFailure
from chia.util.hash import std_hash
from chia.util.keychain import Keychain
from chia.util.logging import TimedDuplicateFilter
from chia.util.profiler import profile_task
from chia.util.task_referencer import create_referenced_task
from chia.wallet.derive_keys import (
    find_authentication_sk,
    find_owner_sk,
    master_sk_to_farmer_sk,
    master_sk_to_pool_sk,
    match_address_to_sk,
)
from chia.wallet.puzzles.singleton_top_layer import SINGLETON_MOD

singleton_mod_hash = SINGLETON_MOD.get_tree_hash()

log = logging.getLogger(__name__)

UPDATE_POOL_INFO_INTERVAL: int = 3600
UPDATE_POOL_INFO_FAILURE_RETRY_INTERVAL: int = 120
UPDATE_POOL_FARMER_INFO_INTERVAL: int = 300


@dataclass(frozen=True)
class GetPoolInfoResult:
    pool_info: dict[str, Any]
    new_pool_url: Optional[str]


def strip_old_entries(pairs: list[tuple[float, Any]], before: float) -> list[tuple[float, Any]]:
    for index, [timestamp, points] in enumerate(pairs):
        if timestamp >= before:
            if index == 0:
                return pairs
            if index > 0:
                return pairs[index:]
    return []


def increment_pool_stats(
    pool_states: dict[bytes32, Any],
    p2_singleton_puzzlehash: bytes32,
    name: str,
    current_time: float,
    count: int = 1,
    value: Optional[Union[int, dict[str, Any]]] = None,
) -> None:
    if p2_singleton_puzzlehash not in pool_states:
        return
    pool_state = pool_states[p2_singleton_puzzlehash]
    if f"{name}_since_start" in pool_state:
        pool_state[f"{name}_since_start"] += count
    if f"{name}_24h" in pool_state:
        if value is None:
            pool_state[f"{name}_24h"].append((uint32(current_time), pool_state["current_difficulty"]))
        else:
            pool_state[f"{name}_24h"].append((uint32(current_time), value))

        # Age out old 24h information for every signage point regardless
        # of any failures.  Note that this still lets old data remain if
        # the client isn't receiving signage points.
        cutoff_24h = current_time - (24 * 60 * 60)
        pool_state[f"{name}_24h"] = strip_old_entries(pairs=pool_state[f"{name}_24h"], before=cutoff_24h)
    return


"""
HARVESTER PROTOCOL (FARMER <-> HARVESTER)
"""


class Farmer:
    if TYPE_CHECKING:
        from chia.rpc.rpc_server import RpcServiceProtocol

        _protocol_check: ClassVar[RpcServiceProtocol] = cast("Farmer", None)

    def __init__(
        self,
        root_path: Path,
        farmer_config: dict[str, Any],
        pool_config: dict[str, Any],
        consensus_constants: ConsensusConstants,
        local_keychain: Optional[Keychain] = None,
    ):
        self.keychain_proxy: Optional[KeychainProxy] = None
        self.local_keychain = local_keychain
        self._root_path = root_path
        self.config = farmer_config
        self.pool_config = pool_config
        # Keep track of all sps, keyed on challenge chain signage point hash
        self.sps: dict[bytes32, list[farmer_protocol.NewSignagePoint]] = {}

        # Keep track of harvester plot identifier (str), target sp index, and PoSpace for each challenge
        self.proofs_of_space: dict[bytes32, list[tuple[str, ProofOfSpace]]] = {}

        # Quality string to plot identifier and challenge_hash, for use with harvester.RequestSignatures
        self.quality_str_to_identifiers: dict[bytes32, tuple[str, bytes32, bytes32, bytes32]] = {}

        # number of responses to each signage point
        self.number_of_responses: dict[bytes32, int] = {}

        # A dictionary of keys to time added. These keys refer to keys in the above 4 dictionaries. This is used
        # to periodically clear the memory
        self.cache_add_time: dict[bytes32, uint64] = {}

        self.plot_sync_receivers: dict[bytes32, Receiver] = {}

        self.cache_clear_task: Optional[asyncio.Task[None]] = None
        self.update_pool_state_task: Optional[asyncio.Task[None]] = None
        self.constants = consensus_constants
        self._shut_down = False
        self.server: Any = None
        self.state_changed_callback: Optional[StateChangedProtocol] = None
        self.log = log
        self.log.addFilter(TimedDuplicateFilter("No pool specific authentication_token_timeout.*", 60 * 10))
        self.log.addFilter(TimedDuplicateFilter("No pool specific difficulty has been set.*", 60 * 10))

        self.started = False
        self.harvester_handshake_task: Optional[asyncio.Task[None]] = None

        # From p2_singleton_puzzle_hash to pool state dict
        self.pool_state: dict[bytes32, dict[str, Any]] = {}

        # From p2_singleton to auth PrivateKey
        self.authentication_keys: dict[bytes32, PrivateKey] = {}

        # Last time we updated pool_state based on the config file
        self.last_config_access_time: float = 0

        self.all_root_sks: list[PrivateKey] = []

        # Use to find missing signage points. (new_signage_point, time)
        self.prev_signage_point: Optional[tuple[uint64, farmer_protocol.NewSignagePoint]] = None

    @contextlib.asynccontextmanager
    async def manage(self) -> AsyncIterator[None]:
        async def start_task() -> None:
            # `Farmer.setup_keys` returns `False` if there are no keys setup yet. In this case we just try until it
            # succeeds or until we need to shut down.
            while not self._shut_down:
                if await self.setup_keys():
                    self.update_pool_state_task = create_referenced_task(self._periodically_update_pool_state_task())
                    self.cache_clear_task = create_referenced_task(self._periodically_clear_cache_and_refresh_task())
                    log.debug("start_task: initialized")
                    self.started = True
                    return
                await asyncio.sleep(1)

        if self.config.get("enable_profiler", False):
            if sys.getprofile() is not None:
                self.log.warning("not enabling profiler, getprofile() is already set")
            else:
                create_referenced_task(profile_task(self._root_path, "farmer", self.log), known_unreferenced=True)

        create_referenced_task(start_task(), known_unreferenced=True)
        try:
            yield
        finally:
            self._shut_down = True

            if self.cache_clear_task is not None:
                await self.cache_clear_task
            if self.update_pool_state_task is not None:
                await self.update_pool_state_task
            if self.keychain_proxy is not None:
                proxy = self.keychain_proxy
                self.keychain_proxy = None
                await proxy.close()
                await asyncio.sleep(0.5)  # https://docs.aiohttp.org/en/stable/client_advanced.html#graceful-shutdown
            self.started = False

    def get_connections(self, request_node_type: Optional[NodeType]) -> list[dict[str, Any]]:
        return default_get_connections(server=self.server, request_node_type=request_node_type)

    async def ensure_keychain_proxy(self) -> KeychainProxy:
        if self.keychain_proxy is None:
            if self.local_keychain:
                self.keychain_proxy = wrap_local_keychain(self.local_keychain, log=self.log)
            else:
                self.keychain_proxy = await connect_to_keychain_and_validate(self._root_path, self.log)
                if not self.keychain_proxy:
                    raise KeychainProxyConnectionFailure
        return self.keychain_proxy

    async def get_all_private_keys(self) -> list[tuple[PrivateKey, bytes]]:
        keychain_proxy = await self.ensure_keychain_proxy()
        return await keychain_proxy.get_all_private_keys()

    async def setup_keys(self) -> bool:
        no_keys_error_str = "No keys exist. Please run 'chia keys generate' or open the UI."
        try:
            self.all_root_sks = [sk for sk, _ in await self.get_all_private_keys()]
        except KeychainProxyConnectionFailure:
            return False

        self._private_keys = [master_sk_to_farmer_sk(sk) for sk in self.all_root_sks] + [
            master_sk_to_pool_sk(sk) for sk in self.all_root_sks
        ]

        if len(self.get_public_keys()) == 0:
            log.warning(no_keys_error_str)
            return False

        config = load_config(self._root_path, "config.yaml")
        if "xch_target_address" not in self.config:
            self.config = config["farmer"]
        if "xch_target_address" not in self.pool_config:
            self.pool_config = config["pool"]
        if "xch_target_address" not in self.config or "xch_target_address" not in self.pool_config:
            log.debug("xch_target_address missing in the config")
            return False

        # This is the farmer configuration
        self.farmer_target_encoded = self.config["xch_target_address"]
        self.farmer_target = decode_puzzle_hash(self.farmer_target_encoded)

        self.pool_public_keys = [G1Element.from_bytes(bytes.fromhex(pk)) for pk in self.config["pool_public_keys"]]

        # This is the self pooling configuration, which is only used for original self-pooled plots
        self.pool_target_encoded = self.pool_config["xch_target_address"]
        self.pool_target = decode_puzzle_hash(self.pool_target_encoded)
        self.pool_sks_map = {bytes(key.get_g1()): key for key in self.get_private_keys()}

        assert len(self.farmer_target) == 32
        assert len(self.pool_target) == 32
        if len(self.pool_sks_map) == 0:
            log.warning(no_keys_error_str)
            return False

        return True

    def _set_state_changed_callback(self, callback: StateChangedProtocol) -> None:
        self.state_changed_callback = callback

    async def on_connect(self, peer: WSChiaConnection) -> None:
        self.state_changed("add_connection", {})

        async def handshake_task() -> None:
            # Wait until the task in `Farmer._start` is done so that we have keys available for the handshake. Bail out
            # early if we need to shut down or if the harvester is not longer connected.
            # TODO: switch to event driven code
            while not self.started and not self._shut_down and peer in self.server.get_connections():  # noqa: ASYNC110
                await asyncio.sleep(1)

            if self._shut_down:
                log.debug("handshake_task: shutdown")
                self.harvester_handshake_task = None
                return

            if peer not in self.server.get_connections():
                log.debug("handshake_task: disconnected")
                self.harvester_handshake_task = None
                return

            # Sends a handshake to the harvester
            handshake = harvester_protocol.HarvesterHandshake(
                self.get_public_keys(),
                self.pool_public_keys,
            )
            msg = make_msg(ProtocolMessageTypes.harvester_handshake, handshake)
            await peer.send_message(msg)
            self.harvester_handshake_task = None

        if peer.connection_type is NodeType.HARVESTER:
            self.plot_sync_receivers[peer.peer_node_id] = Receiver(peer, self.plot_sync_callback)
            self.harvester_handshake_task = create_referenced_task(handshake_task())

    def set_server(self, server: ChiaServer) -> None:
        self.server = server

    def state_changed(self, change: str, data: dict[str, Any]) -> None:
        if self.state_changed_callback is not None:
            self.state_changed_callback(change, data)

    def handle_failed_pool_response(self, p2_singleton_puzzle_hash: bytes32, error_message: str) -> None:
        self.log.error(error_message)
        increment_pool_stats(
            self.pool_state,
            p2_singleton_puzzle_hash,
            "pool_errors",
            time.time(),
            value=ErrorResponse(uint16(PoolErrorCode.REQUEST_FAILED.value), error_message).to_json_dict(),
        )

    async def on_disconnect(self, connection: WSChiaConnection) -> None:
        self.log.info(f"peer disconnected {connection.get_peer_logging()}")
        self.state_changed("close_connection", {})
        if connection.connection_type is NodeType.HARVESTER:
            del self.plot_sync_receivers[connection.peer_node_id]
            self.state_changed("harvester_removed", {"node_id": connection.peer_node_id})

    async def plot_sync_callback(self, peer_id: bytes32, delta: Optional[Delta]) -> None:
        log.debug(f"plot_sync_callback: peer_id {peer_id}, delta {delta}")
        receiver: Receiver = self.plot_sync_receivers[peer_id]
        harvester_updated: bool = delta is not None and not delta.empty()
        if receiver.initial_sync() or harvester_updated:
            self.state_changed("harvester_update", receiver.to_dict(True))

    async def _pool_get_pool_info(self, pool_config: PoolWalletConfig) -> Optional[GetPoolInfoResult]:
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                url = f"{pool_config.pool_url}/pool_info"
                async with session.get(url, ssl=ssl_context_for_root(get_mozilla_ca_crt(), log=self.log)) as resp:
                    if resp.ok:
                        response: dict[str, Any] = json.loads(await resp.text())
                        self.log.info(f"GET /pool_info response: {response}")
                        new_pool_url: Optional[str] = None
                        response_url_str = f"{resp.url}"
                        if (
                            response_url_str != url
                            and len(resp.history) > 0
                            and all(r.status in {301, 308} for r in resp.history)
                        ):
                            new_pool_url = response_url_str.replace("/pool_info", "")

                        return GetPoolInfoResult(pool_info=response, new_pool_url=new_pool_url)
                    else:
                        self.handle_failed_pool_response(
                            pool_config.p2_singleton_puzzle_hash,
                            f"Error in GET /pool_info {pool_config.pool_url}, {resp.status}",
                        )

        except Exception as e:
            self.handle_failed_pool_response(
                pool_config.p2_singleton_puzzle_hash, f"Exception in GET /pool_info {pool_config.pool_url}, {e}"
            )

        return None

    async def _pool_get_farmer(
        self, pool_config: PoolWalletConfig, authentication_token_timeout: uint8, authentication_sk: PrivateKey
    ) -> Optional[dict[str, Any]]:
        authentication_token = get_current_authentication_token(authentication_token_timeout)
        message: bytes32 = std_hash(
            AuthenticationPayload(
                "get_farmer", pool_config.launcher_id, pool_config.target_puzzle_hash, authentication_token
            )
        )
        signature: G2Element = AugSchemeMPL.sign(authentication_sk, message)
        get_farmer_params: dict[str, Union[str, int]] = {
            "launcher_id": pool_config.launcher_id.hex(),
            "authentication_token": authentication_token,
            "signature": bytes(signature).hex(),
        }
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(
                    f"{pool_config.pool_url}/farmer",
                    params=get_farmer_params,
                    ssl=ssl_context_for_root(get_mozilla_ca_crt(), log=self.log),
                ) as resp:
                    if resp.ok:
                        response: dict[str, Any] = json.loads(await resp.text())
                        log_level = logging.INFO
                        if "error_code" in response:
                            log_level = logging.WARNING
                            increment_pool_stats(
                                self.pool_state,
                                pool_config.p2_singleton_puzzle_hash,
                                "pool_errors",
                                time.time(),
                                value=response,
                            )
                        self.log.log(log_level, f"GET /farmer response: {response}")
                        return response
                    else:
                        self.handle_failed_pool_response(
                            pool_config.p2_singleton_puzzle_hash,
                            f"Error in GET /farmer {pool_config.pool_url}, {resp.status}",
                        )
        except Exception as e:
            self.handle_failed_pool_response(
                pool_config.p2_singleton_puzzle_hash, f"Exception in GET /farmer {pool_config.pool_url}, {e}"
            )
        return None

    async def _pool_post_farmer(
        self, pool_config: PoolWalletConfig, authentication_token_timeout: uint8, owner_sk: PrivateKey
    ) -> Optional[dict[str, Any]]:
        auth_sk: Optional[PrivateKey] = self.get_authentication_sk(pool_config)
        assert auth_sk is not None
        post_farmer_payload: PostFarmerPayload = PostFarmerPayload(
            pool_config.launcher_id,
            get_current_authentication_token(authentication_token_timeout),
            auth_sk.get_g1(),
            pool_config.payout_instructions,
            None,
        )
        assert owner_sk.get_g1() == pool_config.owner_public_key
        signature: G2Element = AugSchemeMPL.sign(owner_sk, post_farmer_payload.get_hash())
        post_farmer_request = PostFarmerRequest(post_farmer_payload, signature)
        self.log.debug(f"POST /farmer request {post_farmer_request}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{pool_config.pool_url}/farmer",
                    json=post_farmer_request.to_json_dict(),
                    ssl=ssl_context_for_root(get_mozilla_ca_crt(), log=self.log),
                ) as resp:
                    if resp.ok:
                        response: dict[str, Any] = json.loads(await resp.text())
                        log_level = logging.INFO
                        if "error_code" in response:
                            log_level = logging.WARNING
                            increment_pool_stats(
                                self.pool_state,
                                pool_config.p2_singleton_puzzle_hash,
                                "pool_errors",
                                time.time(),
                                value=response,
                            )
                        self.log.log(log_level, f"POST /farmer response: {response}")
                        return response
                    else:
                        self.handle_failed_pool_response(
                            pool_config.p2_singleton_puzzle_hash,
                            f"Error in POST /farmer {pool_config.pool_url}, {resp.status}",
                        )
        except Exception as e:
            self.handle_failed_pool_response(
                pool_config.p2_singleton_puzzle_hash, f"Exception in POST /farmer {pool_config.pool_url}, {e}"
            )
        return None

    async def _pool_put_farmer(
        self, pool_config: PoolWalletConfig, authentication_token_timeout: uint8, owner_sk: PrivateKey
    ) -> None:
        auth_sk: Optional[PrivateKey] = self.get_authentication_sk(pool_config)
        assert auth_sk is not None
        put_farmer_payload: PutFarmerPayload = PutFarmerPayload(
            pool_config.launcher_id,
            get_current_authentication_token(authentication_token_timeout),
            auth_sk.get_g1(),
            pool_config.payout_instructions,
            None,
        )
        assert owner_sk.get_g1() == pool_config.owner_public_key
        signature: G2Element = AugSchemeMPL.sign(owner_sk, put_farmer_payload.get_hash())
        put_farmer_request = PutFarmerRequest(put_farmer_payload, signature)
        self.log.debug(f"PUT /farmer request {put_farmer_request}")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(
                    f"{pool_config.pool_url}/farmer",
                    json=put_farmer_request.to_json_dict(),
                    ssl=ssl_context_for_root(get_mozilla_ca_crt(), log=self.log),
                ) as resp:
                    if resp.ok:
                        response: dict[str, Any] = json.loads(await resp.text())
                        log_level = logging.INFO
                        if "error_code" in response:
                            log_level = logging.WARNING
                            increment_pool_stats(
                                self.pool_state,
                                pool_config.p2_singleton_puzzle_hash,
                                "pool_errors",
                                time.time(),
                                value=response,
                            )
                        self.log.log(log_level, f"PUT /farmer response: {response}")
                    else:
                        self.handle_failed_pool_response(
                            pool_config.p2_singleton_puzzle_hash,
                            f"Error in PUT /farmer {pool_config.pool_url}, {resp.status}",
                        )
        except Exception as e:
            self.handle_failed_pool_response(
                pool_config.p2_singleton_puzzle_hash, f"Exception in PUT /farmer {pool_config.pool_url}, {e}"
            )

    def get_authentication_sk(self, pool_config: PoolWalletConfig) -> Optional[PrivateKey]:
        if pool_config.p2_singleton_puzzle_hash in self.authentication_keys:
            return self.authentication_keys[pool_config.p2_singleton_puzzle_hash]
        auth_sk: Optional[PrivateKey] = find_authentication_sk(self.all_root_sks, pool_config.owner_public_key)
        if auth_sk is not None:
            self.authentication_keys[pool_config.p2_singleton_puzzle_hash] = auth_sk
        return auth_sk

    async def update_pool_state(self) -> None:
        config = load_config(self._root_path, "config.yaml")

        pool_config_list: list[PoolWalletConfig] = load_pool_config(self._root_path)
        for pool_config in pool_config_list:
            p2_singleton_puzzle_hash = pool_config.p2_singleton_puzzle_hash

            try:
                authentication_sk: Optional[PrivateKey] = self.get_authentication_sk(pool_config)

                if authentication_sk is None:
                    self.log.error(f"Could not find authentication sk for {p2_singleton_puzzle_hash}")
                    continue

                if p2_singleton_puzzle_hash not in self.pool_state:
                    self.pool_state[p2_singleton_puzzle_hash] = {
                        "p2_singleton_puzzle_hash": p2_singleton_puzzle_hash.hex(),
                        "points_found_since_start": 0,
                        "points_found_24h": [],
                        "points_acknowledged_since_start": 0,
                        "points_acknowledged_24h": [],
                        "next_farmer_update": 0,
                        "next_pool_info_update": 0,
                        "current_points": 0,
                        "current_difficulty": None,
                        "pool_errors_24h": [],
                        "valid_partials_since_start": 0,
                        "valid_partials_24h": [],
                        "invalid_partials_since_start": 0,
                        "invalid_partials_24h": [],
                        "insufficient_partials_since_start": 0,
                        "insufficient_partials_24h": [],
                        "stale_partials_since_start": 0,
                        "stale_partials_24h": [],
                        "missing_partials_since_start": 0,
                        "missing_partials_24h": [],
                        "authentication_token_timeout": None,
                        "plot_count": 0,
                        "pool_config": pool_config,
                    }
                    self.log.info(f"Added pool: {pool_config}")
                else:
                    self.pool_state[p2_singleton_puzzle_hash]["pool_config"] = pool_config

                pool_state = self.pool_state[p2_singleton_puzzle_hash]

                # Skip state update when self pooling
                if pool_config.pool_url == "":
                    continue

                enforce_https = config["full_node"]["selected_network"] == "mainnet"
                if enforce_https and not pool_config.pool_url.startswith("https://"):
                    self.log.error(f"Pool URLs must be HTTPS on mainnet {pool_config.pool_url}")
                    continue

                # TODO: Improve error handling below, inform about unexpected failures
                if time.time() >= pool_state["next_pool_info_update"]:
                    pool_state["next_pool_info_update"] = time.time() + UPDATE_POOL_INFO_INTERVAL
                    # Makes a GET request to the pool to get the updated information
                    pool_info_result = await self._pool_get_pool_info(pool_config)
                    if pool_info_result is not None and "error_code" not in pool_info_result.pool_info:
                        pool_info = pool_info_result.pool_info
                        pool_state["authentication_token_timeout"] = pool_info["authentication_token_timeout"]
                        # Only update the first time from GET /pool_info, gets updated from GET /farmer later
                        if pool_state["current_difficulty"] is None:
                            pool_state["current_difficulty"] = pool_info["minimum_difficulty"]
                    else:
                        pool_state["next_pool_info_update"] = time.time() + UPDATE_POOL_INFO_FAILURE_RETRY_INTERVAL

                    if pool_info_result is not None and pool_info_result.new_pool_url is not None:
                        update_pool_url(self._root_path, pool_config, pool_info_result.new_pool_url)

                if time.time() >= pool_state["next_farmer_update"]:
                    pool_state["next_farmer_update"] = time.time() + UPDATE_POOL_FARMER_INFO_INTERVAL
                    authentication_token_timeout = pool_state["authentication_token_timeout"]

                    async def update_pool_farmer_info() -> tuple[Optional[GetFarmerResponse], Optional[PoolErrorCode]]:
                        # Run a GET /farmer to see if the farmer is already known by the pool
                        response = await self._pool_get_farmer(
                            pool_config, authentication_token_timeout, authentication_sk
                        )
                        farmer_response: Optional[GetFarmerResponse] = None
                        error_code_response: Optional[PoolErrorCode] = None
                        if response is not None:
                            if "error_code" not in response:
                                farmer_response = GetFarmerResponse.from_json_dict(response)
                                if farmer_response is not None:
                                    pool_state["current_difficulty"] = farmer_response.current_difficulty
                                    pool_state["current_points"] = farmer_response.current_points
                            else:
                                try:
                                    error_code_response = PoolErrorCode(response["error_code"])
                                except ValueError:
                                    self.log.error(
                                        f"Invalid error code received from the pool: {response['error_code']}"
                                    )

                        return farmer_response, error_code_response

                    if authentication_token_timeout is not None:
                        farmer_info, error_code = await update_pool_farmer_info()
                        if error_code == PoolErrorCode.FARMER_NOT_KNOWN:
                            # Make the farmer known on the pool with a POST /farmer
                            owner_sk_and_index = find_owner_sk(self.all_root_sks, pool_config.owner_public_key)
                            assert owner_sk_and_index is not None
                            post_response = await self._pool_post_farmer(
                                pool_config, authentication_token_timeout, owner_sk_and_index[0]
                            )
                            if post_response is not None and "error_code" not in post_response:
                                self.log.info(
                                    f"Welcome message from {pool_config.pool_url}: {post_response['welcome_message']}"
                                )
                                # Now we should be able to update the local farmer info
                                farmer_info, farmer_is_known = await update_pool_farmer_info()
                                if farmer_info is None and not farmer_is_known:
                                    self.log.error("Failed to update farmer info after POST /farmer.")

                        # Update the farmer information on the pool if the payout instructions changed or if the
                        # signature is invalid (latter to make sure the pool has the correct authentication public key).
                        payout_instructions_update_required: bool = (
                            farmer_info is not None
                            and pool_config.payout_instructions.lower() != farmer_info.payout_instructions.lower()
                        )
                        if payout_instructions_update_required or error_code == PoolErrorCode.INVALID_SIGNATURE:
                            owner_sk_and_index = find_owner_sk(self.all_root_sks, pool_config.owner_public_key)
                            assert owner_sk_and_index is not None
                            await self._pool_put_farmer(
                                pool_config, authentication_token_timeout, owner_sk_and_index[0]
                            )
                    else:
                        self.log.warning(
                            f"No pool specific authentication_token_timeout has been set for {p2_singleton_puzzle_hash}"
                            f", check communication with the pool."
                        )

            except Exception as e:
                tb = traceback.format_exc()
                self.log.error(f"Exception in update_pool_state for {pool_config.pool_url}, {e} {tb}")

    def get_public_keys(self) -> list[G1Element]:
        return [child_sk.get_g1() for child_sk in self._private_keys]

    def get_private_keys(self) -> list[PrivateKey]:
        return self._private_keys

    async def get_reward_targets(self, search_for_private_key: bool, max_ph_to_search: int = 500) -> dict[str, Any]:
        if search_for_private_key:
            all_sks = await self.get_all_private_keys()
            have_farmer_sk, have_pool_sk = False, False
            search_addresses: list[bytes32] = [self.farmer_target, self.pool_target]
            for sk, _ in all_sks:
                found_addresses: set[bytes32] = match_address_to_sk(sk, search_addresses, max_ph_to_search)

                if not have_farmer_sk and self.farmer_target in found_addresses:
                    search_addresses.remove(self.farmer_target)
                    have_farmer_sk = True

                if not have_pool_sk and self.pool_target in found_addresses:
                    search_addresses.remove(self.pool_target)
                    have_pool_sk = True

                if have_farmer_sk and have_pool_sk:
                    break

            return {
                "farmer_target": self.farmer_target_encoded,
                "pool_target": self.pool_target_encoded,
                "have_farmer_sk": have_farmer_sk,
                "have_pool_sk": have_pool_sk,
            }
        return {
            "farmer_target": self.farmer_target_encoded,
            "pool_target": self.pool_target_encoded,
        }

    def set_reward_targets(self, farmer_target_encoded: Optional[str], pool_target_encoded: Optional[str]) -> None:
        with lock_and_load_config(self._root_path, "config.yaml") as config:
            if farmer_target_encoded is not None:
                self.farmer_target_encoded = farmer_target_encoded
                self.farmer_target = decode_puzzle_hash(farmer_target_encoded)
                config["farmer"]["xch_target_address"] = farmer_target_encoded
            if pool_target_encoded is not None:
                self.pool_target_encoded = pool_target_encoded
                self.pool_target = decode_puzzle_hash(pool_target_encoded)
                config["pool"]["xch_target_address"] = pool_target_encoded
            save_config(self._root_path, "config.yaml", config)

    async def set_payout_instructions(self, launcher_id: bytes32, payout_instructions: str) -> None:
        for p2_singleton_puzzle_hash, pool_state_dict in self.pool_state.items():
            if launcher_id == pool_state_dict["pool_config"].launcher_id:
                with lock_and_load_config(self._root_path, "config.yaml") as config:
                    new_list = []
                    pool_list = config["pool"].get("pool_list", [])
                    if pool_list is not None:
                        for list_element in pool_list:
                            if hexstr_to_bytes(list_element["launcher_id"]) == bytes(launcher_id):
                                list_element["payout_instructions"] = payout_instructions
                            new_list.append(list_element)

                    config["pool"]["pool_list"] = new_list
                    save_config(self._root_path, "config.yaml", config)
                # Force a GET /farmer which triggers the PUT /farmer if it detects the changed instructions
                pool_state_dict["next_farmer_update"] = 0
                return

        self.log.warning(f"Launcher id: {launcher_id} not found")

    async def generate_login_link(self, launcher_id: bytes32) -> Optional[str]:
        for pool_state in self.pool_state.values():
            pool_config: PoolWalletConfig = pool_state["pool_config"]
            if pool_config.launcher_id != launcher_id:
                continue

            authentication_sk: Optional[PrivateKey] = self.get_authentication_sk(pool_config)
            if authentication_sk is None:
                self.log.error(f"Could not find authentication sk for {pool_config.p2_singleton_puzzle_hash}")
                continue
            authentication_token_timeout = pool_state["authentication_token_timeout"]
            if authentication_token_timeout is None:
                self.log.error(
                    f"No pool specific authentication_token_timeout has been set for"
                    f"{pool_config.p2_singleton_puzzle_hash}, check communication with the pool."
                )
                return None

            authentication_token = get_current_authentication_token(authentication_token_timeout)
            message: bytes32 = std_hash(
                AuthenticationPayload(
                    "get_login", pool_config.launcher_id, pool_config.target_puzzle_hash, authentication_token
                )
            )
            signature: G2Element = AugSchemeMPL.sign(authentication_sk, message)
            return (
                pool_config.pool_url
                + f"/login?launcher_id={launcher_id.hex()}&authentication_token={authentication_token}"
                f"&signature={bytes(signature).hex()}"
            )

        return None

    async def get_harvesters(self, counts_only: bool = False) -> dict[str, Any]:
        harvesters: list[dict[str, Any]] = []
        for connection in self.server.get_connections(NodeType.HARVESTER):
            self.log.debug(f"get_harvesters host: {connection.peer_info.host}, node_id: {connection.peer_node_id}")
            receiver = self.plot_sync_receivers.get(connection.peer_node_id)
            if receiver is not None:
                harvesters.append(receiver.to_dict(counts_only))
            else:
                self.log.debug(
                    f"get_harvesters invalid peer: {connection.peer_info.host}, node_id: {connection.peer_node_id}"
                )

        return {"harvesters": harvesters}

    def get_receiver(self, node_id: bytes32) -> Receiver:
        receiver: Optional[Receiver] = self.plot_sync_receivers.get(node_id)
        if receiver is None:
            raise KeyError(f"Receiver missing for {node_id}")
        return receiver

    def check_missing_signage_points(
        self, timestamp: uint64, new_signage_point: farmer_protocol.NewSignagePoint
    ) -> Optional[tuple[uint64, uint32]]:
        if self.prev_signage_point is None:
            self.prev_signage_point = (timestamp, new_signage_point)
            return None

        prev_time, prev_sp = self.prev_signage_point
        self.prev_signage_point = (timestamp, new_signage_point)

        if prev_sp.challenge_hash == new_signage_point.challenge_hash:
            missing_sps = new_signage_point.signage_point_index - prev_sp.signage_point_index - 1
            if missing_sps > 0:
                return timestamp, uint32(missing_sps)
            return None

        actual_sp_interval_seconds = float(timestamp - prev_time)
        if actual_sp_interval_seconds <= 0:
            return None

        expected_sp_interval_seconds = self.constants.SUB_SLOT_TIME_TARGET / self.constants.NUM_SPS_SUB_SLOT
        allowance = 1.6  # Should be chosen from the range (1 <= allowance < 2)
        if actual_sp_interval_seconds < expected_sp_interval_seconds * allowance:
            return None

        skipped_sps = uint32(floor(actual_sp_interval_seconds / expected_sp_interval_seconds))
        return timestamp, skipped_sps

    async def _periodically_update_pool_state_task(self) -> None:
        time_slept = 0
        config_path: Path = config_path_for_filename(self._root_path, "config.yaml")
        while not self._shut_down:
            # Every time the config file changes, read it to check the pool state
            stat_info = config_path.stat()
            if stat_info.st_mtime > self.last_config_access_time:
                # If we detect the config file changed, refresh private keys first just in case
                self.all_root_sks = [sk for sk, _ in await self.get_all_private_keys()]
                self.last_config_access_time = stat_info.st_mtime
                await self.update_pool_state()
                time_slept = 0
            elif time_slept > 60:
                await self.update_pool_state()
                time_slept = 0
            time_slept += 1
            await asyncio.sleep(1)

    async def _periodically_clear_cache_and_refresh_task(self) -> None:
        time_slept = 0
        refresh_slept = 0
        while not self._shut_down:
            try:
                if time_slept > self.constants.SUB_SLOT_TIME_TARGET:
                    now = time.time()
                    removed_keys: list[bytes32] = []
                    for key, add_time in self.cache_add_time.items():
                        if now - float(add_time) > self.constants.SUB_SLOT_TIME_TARGET * 3:
                            self.sps.pop(key, None)
                            self.proofs_of_space.pop(key, None)
                            self.quality_str_to_identifiers.pop(key, None)
                            self.number_of_responses.pop(key, None)
                            removed_keys.append(key)
                    for key in removed_keys:
                        self.cache_add_time.pop(key, None)
                    time_slept = 0
                    log.debug(
                        f"Cleared farmer cache. Num sps: {len(self.sps)} {len(self.proofs_of_space)} "
                        f"{len(self.quality_str_to_identifiers)} {len(self.number_of_responses)}"
                    )
                time_slept += 1
                refresh_slept += 1
                # Periodically refresh GUI to show the correct download/upload rate.
                if refresh_slept >= 30:
                    self.state_changed("add_connection", {})
                    refresh_slept = 0

            except Exception:
                log.error(f"_periodically_clear_cache_and_refresh_task failed: {traceback.format_exc()}")

            await asyncio.sleep(1)

    def notify_farmer_reward_taken_by_harvester_as_fee(
        self, sp: farmer_protocol.NewSignagePoint, proof_of_space: harvester_protocol.NewProofOfSpace
    ) -> None:
        """
        Apply a fee quality convention (see CHIP-22: https://github.com/Chia-Network/chips/pull/88)
        given the proof and signage point. This will be tested against the fee threshold reported
        by the harvester (if any), and logged.
        """
        assert proof_of_space.farmer_reward_address_override is not None

        challenge_str = str(sp.challenge_hash)

        ph_prefix = self.config["network_overrides"]["config"][self.config["selected_network"]]["address_prefix"]
        farmer_reward_puzzle_hash = encode_puzzle_hash(proof_of_space.farmer_reward_address_override, ph_prefix)

        self.log.info(
            f"Farmer reward for challenge '{challenge_str}' "
            + f"taken by harvester for reward address '{farmer_reward_puzzle_hash}'"
        )

        fee_quality = calculate_harvester_fee_quality(proof_of_space.proof.proof, sp.challenge_hash)
        fee_quality_rate = float(fee_quality) / float(0xFFFFFFFF) * 100.0

        if proof_of_space.fee_info is not None:
            fee_threshold = proof_of_space.fee_info.applied_fee_threshold
            fee_threshold_rate = float(fee_threshold) / float(0xFFFFFFFF) * 100.0

            if fee_quality <= fee_threshold:
                self.log.info(
                    f"Fee threshold passed for challenge '{challenge_str}': "
                    + f"{fee_quality_rate:.3f}%/{fee_threshold_rate:.3f}% ({fee_quality}/{fee_threshold})"
                )
            else:
                self.log.warning(
                    f"Invalid fee threshold for challenge '{challenge_str}': "
                    + f"{fee_quality_rate:.3f}%/{fee_threshold_rate:.3f}% ({fee_quality}/{fee_threshold})"
                )
                self.log.warning(
                    "Harvester illegitimately took a fee reward that "
                    + "did not belong to it or it incorrectly applied the fee convention."
                )
        else:
            self.log.warning(
                "Harvester illegitimately took reward by failing to provide its fee rate "
                + f"for challenge '{challenge_str}'. "
                + f"Fee quality was {fee_quality_rate:.3f}% ({fee_quality} or 0x{fee_quality:08x})"
            )


def calculate_harvester_fee_quality(proof: bytes, challenge: bytes32) -> uint32:
    """
    This calculates the 'fee quality' given a convention between farmers and third party harvesters.
    See CHIP-22: https://github.com/Chia-Network/chips/pull/88
    """
    return uint32(int.from_bytes(std_hash(proof + challenge)[32 - 4 :], byteorder="big", signed=False))
