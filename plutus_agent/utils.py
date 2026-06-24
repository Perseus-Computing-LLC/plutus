"""Utility functions for Plutus."""
from __future__ import annotations


def strict_int(value) -> int:
    """Parse an integer strictly, rejecting floats and non-integer strings.
    
    Accepts:
        - int: 5, 0, -10
        - str with integer value: '5', '0', '-10'
    
    Rejects:
        - float: 1.9, 1.0
        - str with float: '1.9', '1.0'
    
    Raises:
        ValueError: if value is a float or contains a decimal point
        TypeError: if value cannot be converted to int
    """
    if isinstance(value, float):
        raise ValueError(f"strict_int: float {value} not allowed (would truncate)")
    if isinstance(value, str):
        if '.' in value:
            raise ValueError(f"strict_int: string '{value}' contains decimal point")
        return int(value)
    return int(value)
