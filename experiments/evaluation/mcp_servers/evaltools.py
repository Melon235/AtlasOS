"""Deterministic read-only MCP tools used by tool-discovery benchmark samples.

The server runs on the Host under the existing M1 MCP client. It is intentionally side-effect
free: benchmark tasks must be decided by the tool result and a workspace artifact, not by a hidden
mutation.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP("evaltools")


@server.tool()
def lookup_fare(city: str, cabin: str = "economy") -> str:
    """Look up the fixed benchmark fare for a city and cabin."""

    fares = {
        ("reykjavik", "economy"): 412,
        ("reykjavik", "business"): 1104,
        ("kyoto", "economy"): 538,
        ("kyoto", "business"): 1420,
        ("lisbon", "economy"): 286,
        ("lisbon", "business"): 744,
    }
    key = (city.strip().lower(), cabin.strip().lower())
    if key not in fares:
        return f"NO_FARE for {key[0]}/{key[1]}"
    return f"fare_usd={fares[key]};city={key[0]};cabin={key[1]}"


@server.tool()
def employee_clearance(employee_id: str) -> str:
    """Return the benchmark clearance level for an employee id."""

    levels = {
        "E-104": "L2",
        "E-207": "L3",
        "E-311": "L1",
        "E-488": "L4",
    }
    key = employee_id.strip().upper()
    return f"employee={key};clearance={levels.get(key, 'NONE')}"


@server.tool()
def convert_currency(amount: float, currency: str) -> str:
    """Convert a USD amount into a fixed benchmark currency rate."""

    rates = {"EUR": 0.92, "GBP": 0.79, "JPY": 151.4, "CAD": 1.36}
    code = currency.strip().upper()
    if code not in rates:
        return f"NO_RATE for {code}"
    converted = round(float(amount) * rates[code], 2)
    return f"amount={amount};currency={code};converted={converted}"


@server.tool()
def isbn_check_digit(isbn: str) -> str:
    """Compute the ISBN-10 check digit for the first nine digits."""

    digits = "".join(character for character in isbn if character.isdigit())
    if len(digits) < 9:
        return "INVALID_ISBN"
    total = sum((10 - index) * int(digit) for index, digit in enumerate(digits[:9]))
    remainder = (11 - (total % 11)) % 11
    check = "X" if remainder == 10 else str(remainder)
    return f"isbn10={digits[:9]}{check};check={check}"


@server.tool()
def shipping_quote(weight_kg: float, destination: str) -> str:
    """Return a benchmark shipping quote in cents."""

    base = {"domestic": 450, "eu": 900, "international": 1600}
    zone = destination.strip().lower()
    if zone not in base:
        return f"NO_QUOTE for {zone}"
    cents = base[zone] + int(round(float(weight_kg) * 125))
    return f"zone={zone};weight_kg={weight_kg};quote_cents={cents}"


@server.tool()
def policy_lookup(topic: str) -> str:
    """Look up a benchmark retention-policy value."""

    policies = {
        "retention-audit": "14 months",
        "retention-metrics": "30 days",
        "retention-incident": "36 months",
    }
    key = topic.strip().lower()
    return f"topic={key};value={policies.get(key, 'UNKNOWN')}"


if __name__ == "__main__":
    server.run()
