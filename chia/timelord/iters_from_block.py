from __future__ import annotations

from typing import Optional, Union

from chia_rs import ConsensusConstants, RewardChainBlock, RewardChainBlockUnfinished
from chia_rs.sized_bytes import bytes32
from chia_rs.sized_ints import uint32, uint64

from chia.consensus.pot_iterations import calculate_ip_iters, calculate_iterations_quality, calculate_sp_iters
from chia.types.blockchain_format.proof_of_space import verify_and_get_quality_string


def iters_from_block(
    constants: ConsensusConstants,
    reward_chain_block: Union[RewardChainBlock, RewardChainBlockUnfinished],
    sub_slot_iters: uint64,
    difficulty: uint64,
    height: uint32,
) -> tuple[uint64, uint64]:
    if reward_chain_block.challenge_chain_sp_vdf is None:
        assert reward_chain_block.signage_point_index == 0
        cc_sp: bytes32 = reward_chain_block.pos_ss_cc_challenge_hash
    else:
        cc_sp = reward_chain_block.challenge_chain_sp_vdf.output.get_hash()

    quality_string: Optional[bytes32] = verify_and_get_quality_string(
        reward_chain_block.proof_of_space,
        constants,
        reward_chain_block.pos_ss_cc_challenge_hash,
        cc_sp,
        height=height,
    )
    assert quality_string is not None

    # TODO: support v2 plots
    pos_size_v1 = reward_chain_block.proof_of_space.size_v1()
    assert pos_size_v1 is not None, "plot format v2 not supported yet"

    required_iters: uint64 = calculate_iterations_quality(
        constants.DIFFICULTY_CONSTANT_FACTOR,
        quality_string,
        pos_size_v1,
        difficulty,
        cc_sp,
    )
    return (
        calculate_sp_iters(constants, sub_slot_iters, reward_chain_block.signage_point_index),
        calculate_ip_iters(
            constants,
            sub_slot_iters,
            reward_chain_block.signage_point_index,
            required_iters,
        ),
    )
