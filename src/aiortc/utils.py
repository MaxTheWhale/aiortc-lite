import os
from struct import unpack


def random32() -> int:
    return unpack("!L", os.urandom(4))[0]


def uint16_add(a: int, b: int) -> int:
    """
    Return a + b.
    """
    return (a + b) & 0xFFFF


def uint16_gt(a: int, b: int) -> bool:
    """
    Return a > b.
    """
    half_mod = 0x8000
    return ((a < b) and ((b - a) > half_mod)) or ((a > b) and ((a - b) < half_mod))


def uint32_gt(a: int, b: int) -> bool:
    """
    Return a > b.
    """
    half_mod = 0x80000000
    return ((a < b) and ((b - a) > half_mod)) or ((a > b) and ((a - b) < half_mod))


def uint32_gte(a: int, b: int) -> bool:
    """
    Return a >= b.
    """
    return (a == b) or uint32_gt(a, b)
