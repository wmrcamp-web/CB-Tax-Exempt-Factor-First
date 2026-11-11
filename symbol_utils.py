"""Shared symbol normalization helpers."""


def normalize_symbol(symbol: str) -> str:
    return symbol.replace(".", "-")
