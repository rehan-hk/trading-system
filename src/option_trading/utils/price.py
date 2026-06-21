"""Price rounding utilities for NSE index options."""


def round_to_nearest_fifty(x: float) -> int:
    """Round a price to the nearest 50 (used for NIFTY and FINNIFTY strikes)."""
    return int(round(x / 50.0) * 50)


def round_to_nearest_hundred(x: float) -> int:
    """Round a price to the nearest 100 (used for BANKNIFTY and MIDCPNIFTY strikes)."""
    return int(round(x / 100.0) * 100)


def round_price_based_on_template(spot_price: float, index_symbol: str) -> int:
    """
    Round spot price to the appropriate strike interval for the given index.

    Args:
        spot_price: Raw LTP from Kite.
        index_symbol: Short symbol e.g. 'BANKNIFTY', 'NIFTY', 'FINNIFTY', 'MIDCPNIFTY'.

    Returns:
        Rounded integer strike price.
    """
    if index_symbol in ("BANKNIFTY", "MIDCPNIFTY"):
        return round_to_nearest_hundred(spot_price)
    elif index_symbol in ("NIFTY", "FINNIFTY"):
        return round_to_nearest_fifty(spot_price)
    else:
        # Fallback: no rounding for unknown symbols
        return int(round(spot_price))
