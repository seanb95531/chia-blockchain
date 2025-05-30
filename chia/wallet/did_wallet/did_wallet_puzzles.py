from __future__ import annotations

from collections.abc import Iterator
from typing import Optional, Union

from chia_puzzles_py.programs import DID_INNERPUZ, DID_INNERPUZ_HASH, NFT_INTERMEDIATE_LAUNCHER
from chia_rs import CoinSpend, G1Element
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint64

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.program import Program
from chia.types.coin_spend import make_spend
from chia.types.condition_opcodes import ConditionOpcode
from chia.wallet.singleton import (
    SINGLETON_LAUNCHER_PUZZLE_HASH,
    SINGLETON_LAUNCHER_PUZZLE_HASH_TREE_HASH,
    SINGLETON_TOP_LAYER_MOD,
    SINGLETON_TOP_LAYER_MOD_HASH,
    SINGLETON_TOP_LAYER_MOD_HASH_TREE_HASH,
    is_singleton,
)
from chia.wallet.util.curry_and_treehash import (
    calculate_hash_of_quoted_mod_hash,
    curry_and_treehash,
    shatree_atom,
    shatree_atom_list,
    shatree_int,
    shatree_pair,
)

DID_INNERPUZ_MOD = Program.from_bytes(DID_INNERPUZ)
DID_INNERPUZ_MOD_HASH = bytes32(DID_INNERPUZ_HASH)
DID_INNERPUZ_MOD_HASH_QUOTED = calculate_hash_of_quoted_mod_hash(DID_INNERPUZ_MOD_HASH)
INTERMEDIATE_LAUNCHER_MOD = Program.from_bytes(NFT_INTERMEDIATE_LAUNCHER)


def create_innerpuz(
    p2_puzzle_or_hash: Union[Program, bytes32],
    recovery_list: list[bytes32],
    num_of_backup_ids_needed: uint64,
    launcher_id: bytes32,
    metadata: Program = Program.to([]),
    recovery_list_hash: Optional[Program] = None,
) -> Program:
    """
    Create DID inner puzzle
    :param p2_puzzle_or_hash: Standard P2 puzzle or hash
    :param recovery_list: A list of DIDs used for the recovery
    :param num_of_backup_ids_needed: Need how many DIDs for the recovery
    :param launcher_id: ID of the launch coin
    :param metadata: DID customized metadata
    :param recovery_list_hash: Recovery list hash
    :return: DID inner puzzle
    Note: Receiving a standard P2 puzzle hash wouldn't calculate a valid puzzle, but
    that can be useful if calling `.get_tree_hash_precalc()` on it.
    """
    backup_ids_hash: Union[Program, bytes32] = Program.to(recovery_list).get_tree_hash()
    if recovery_list_hash is not None:
        backup_ids_hash = recovery_list_hash
    singleton_struct = Program.to((SINGLETON_TOP_LAYER_MOD_HASH, (launcher_id, SINGLETON_LAUNCHER_PUZZLE_HASH)))
    return DID_INNERPUZ_MOD.curry(
        p2_puzzle_or_hash, backup_ids_hash, num_of_backup_ids_needed, singleton_struct, metadata
    )


def get_inner_puzhash_by_p2(
    p2_puzhash: bytes32,
    num_of_backup_ids_needed: uint64,
    launcher_id: bytes32,
    metadata: Program = Program.to([]),
    recovery_list: Optional[list[bytes32]] = None,
    recovery_list_hash: Optional[Program] = None,
) -> bytes32:
    """
    Calculate DID inner puzzle hash based on a P2 puzzle hash
    :param p2_puzhash: P2 puzzle hash
    :param recovery_list: A list of DIDs used for the recovery
    :param num_of_backup_ids_needed: Need how many DIDs for the recovery
    :param launcher_id: ID of the launch coin
    :param metadata: DID customized metadata
    :return: DID inner puzzle hash
    """

    if recovery_list is None and recovery_list_hash is None:
        raise ValueError("Cannot construct DID inner puzzle without information about recovery list")

    # Allow both recovery_list and recovery_list_hash to be provided but
    # in that case the list is ignored and the hash is used
    # this matches the behaviour of create_innerpuz
    if recovery_list_hash is not None:
        backup_ids_hash = recovery_list_hash.as_atom()
    elif recovery_list is not None:
        backup_ids_hash = shatree_atom_list(recovery_list)

    # singleton_struct = (MOD_HASH . (LAUNCHER_ID . LAUNCHER_PUZZLE_HASH))
    singleton_struct = shatree_pair(
        SINGLETON_TOP_LAYER_MOD_HASH_TREE_HASH,
        shatree_pair(shatree_atom(launcher_id), SINGLETON_LAUNCHER_PUZZLE_HASH_TREE_HASH),
    )

    return curry_and_treehash(
        DID_INNERPUZ_MOD_HASH_QUOTED,
        p2_puzhash,
        shatree_atom(backup_ids_hash),
        shatree_int(num_of_backup_ids_needed),
        singleton_struct,
        metadata.get_tree_hash(),
    )


def is_did_innerpuz(inner_f: Program) -> bool:
    """
    Check if a puzzle is a DID inner mode
    :param inner_f: puzzle
    :return: Boolean
    """
    return inner_f == DID_INNERPUZ_MOD


def uncurry_innerpuz(puzzle: Program) -> Optional[tuple[Program, Program, Program, Program, Program]]:
    """
    Uncurry a DID inner puzzle
    :param puzzle: DID puzzle
    :return: Curried parameters
    """
    r = puzzle.uncurry()
    if r is None:
        return r
    inner_f, args = r
    if not is_did_innerpuz(inner_f):
        return None

    p2_puzzle, id_list, num_of_backup_ids_needed, singleton_struct, metadata = list(args.as_iter())
    return p2_puzzle, id_list, num_of_backup_ids_needed, singleton_struct, metadata


def create_recovery_message_puzzle(recovering_coin_id: bytes32, newpuz: bytes32, pubkey: G1Element) -> Program:
    """
    Create attestment message puzzle
    :param recovering_coin_id: ID of the DID coin needs to recover
    :param newpuz: New wallet puzzle hash
    :param pubkey: New wallet pubkey
    :return: Message puzzle
    """
    puzzle = Program.to(
        (
            1,
            [
                [ConditionOpcode.CREATE_COIN_ANNOUNCEMENT, recovering_coin_id],
                [ConditionOpcode.AGG_SIG_UNSAFE, bytes(pubkey), newpuz],
            ],
        )
    )
    return puzzle


def create_spend_for_message(
    parent_of_message: bytes32, recovering_coin: bytes32, newpuz: bytes32, pubkey: G1Element
) -> CoinSpend:
    """
    Create a CoinSpend for a atestment
    :param parent_of_message: Parent coin ID
    :param recovering_coin: ID of the DID coin needs to recover
    :param newpuz: New wallet puzzle hash
    :param pubkey: New wallet pubkey
    :return: CoinSpend
    """
    puzzle = create_recovery_message_puzzle(recovering_coin, newpuz, pubkey)
    coin = Coin(parent_of_message, puzzle.get_tree_hash(), uint64(0))
    solution = Program.to([])
    return make_spend(coin, puzzle, solution)


def match_did_puzzle(mod: Program, curried_args: Program) -> Optional[Iterator[Program]]:
    """
    Given a puzzle test if it's a DID, if it is, return the curried arguments
    :param puzzle: Puzzle
    :return: Curried parameters
    """
    try:
        if mod == SINGLETON_TOP_LAYER_MOD:
            mod, curried_args = curried_args.rest().first().uncurry()
            if mod == DID_INNERPUZ_MOD:
                return curried_args.as_iter()
    except Exception:
        import traceback

        print(f"exception: {traceback.format_exc()}")
    return None


def check_is_did_puzzle(puzzle: Program) -> bool:
    """
    Check if a puzzle is a DID puzzle
    :param puzzle: Puzzle
    :return: Boolean
    """
    r = puzzle.uncurry()
    if r is None:
        return False
    inner_f, _ = r
    return is_singleton(inner_f)


def metadata_to_program(metadata: dict[str, str]) -> Program:
    """
    Convert the metadata dict to a Chialisp program
    :param metadata: User defined metadata
    :return: Chialisp program
    """
    kv_list = []
    for key, value in metadata.items():
        kv_list.append((key, value))
    return Program.to(kv_list)


def did_program_to_metadata(program: Program) -> dict[str, str]:
    """
    Convert a program to a metadata dict
    :param program: Chialisp program contains the metadata
    :return: Metadata dict
    """
    metadata = {}
    for key, val in program.as_python():
        metadata[str(key, "utf-8")] = str(val, "utf-8")
    return metadata
