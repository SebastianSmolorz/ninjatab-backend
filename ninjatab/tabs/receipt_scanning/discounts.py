"""Attach item-level discounts to their item, using the OCR line order.

A supermarket receipt prints a promotion on the line *under* the item it reduces:

    1 Monster Energy Drink Ultra £6.40
    4x500ml
    Cc £5.25 -£1.15

Position is the whole signal. Nothing here reads the "YOUR SAVINGS TODAY"
summary that some receipts print at the bottom, because plenty of receipts print
no such line - a trailing promotion is *discarded* rather than relied upon.

The model already attaches discounts itself on most calls, but not on all of
them, and consensus selection is free to pick a call that lumped them into
`other_charges` instead. `vote_discounts` settles that across the run's calls:
a discount is real when most calls read the same amount under the same item.
"""

import difflib
import logging
import re
from collections import Counter

logger = logging.getLogger("app")

# A promotion line names its scheme. Matching on wording rather than on a minus
# sign is deliberate: Mistral drops the sign on Sainsbury's receipts, which print
# "Nectar Price Saving £0.60" in the markdown for what is really -£0.60.
PROMO = re.compile(
    r"nectar price saving|price saving|clubcard|\bcc\b|special offer|multi-?save"
    r"|meal deal|promotion|discount|saving|\boffer\b",
    re.I,
)

# Where the item list ends. Past this, a promotion belongs to no item.
TOTALS = re.compile(
    r"balance due|amount due|\bdue\b|\btotal\b|mastercard|visa|\bcard\b"
    r"|\bcash\b|\bchange\b|\bpaid\b",
    re.I,
)

MONEY = re.compile(r"(-)?\s*[£$€]\s*(-)?(\d+(?:[.,]\d{1,2})?)")

# How much of the shorter name has to match for a promotion to bind to an item.
MATCH_RATIO = 0.5


def _amounts(line: str) -> list[tuple[float, bool]]:
    """[(value, was_negative)] for every money token on the line."""
    found = []
    for match in MONEY.finditer(line):
        value = float(match.group(3).replace(",", "."))
        found.append((value, bool(match.group(1) or match.group(2))))
    return found


def _discount_amount(line: str) -> float | None:
    """The reduction a promotion line states, always returned negative.

    A Clubcard line carries two figures - "Cc £5.25 -£1.15" is the new price and
    the saving - so an explicitly negative token wins. Sainsbury's prints one
    figure and no sign, in which case the only token is the saving.
    """
    found = _amounts(line)
    if not found:
        return None
    negative = [value for value, was_negative in found if was_negative]
    if negative:
        return -abs(negative[0])
    if len(found) == 1:
        return -abs(found[0][0])
    # Several unsigned figures: too ambiguous to guess which is the reduction.
    return None


def _tokens(text: str) -> set:
    """Words worth matching on: letters only, so prices and quantities drop out."""
    return {w for w in re.sub(r"[^a-z ]+", " ", text.lower()).split() if len(w) > 2}


def _similar(a: str, b: str) -> float:
    """Overlap of the shorter token set, falling back to character similarity
    for the short all-caps names that Sainsbury's prints ("TTD PAELLA 400G")."""
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb:
        return len(ta & tb) / min(len(ta), len(tb))
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def parse_line_discounts(markdown: str) -> list[dict]:
    """Promotions found under an item, in receipt order.

    Returns [{"item_text", "name", "amount"}]. Stops at the totals block, so a
    summary line printed below the total is never returned at all.
    """
    discounts: list[dict] = []
    current: str | None = None
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # A totals line carries a figure. Requiring one stops a markdown table's
        # "| Qty | Item | Price | Total |" header from ending the item list
        # before it has begun - which silenced this parser entirely on every
        # receipt Mistral chose to render as a table.
        if TOTALS.search(line) and _amounts(line):
            break
        if PROMO.search(line):
            amount = _discount_amount(line)
            if amount and current:
                discounts.append({
                    "item_text": current,
                    "name": PROMO.search(line).group(0).strip(),
                    "amount": amount,
                })
            continue
        if _amounts(line):
            current = line  # a priced line is an item; unpriced ones continue it
    return discounts


def vote_discounts(markdowns: list[str], quorum: int | None = None) -> list[dict]:
    """The discounts a majority of the run's calls agree on.

    Keyed on (item, amount): calls that disagree on either produce separate keys,
    so a figure only one call saw is dropped rather than averaged into existence.
    """
    if not markdowns:
        return []
    quorum = quorum if quorum is not None else len(markdowns) // 2 + 1
    counts: Counter = Counter()
    seen: dict = {}
    for markdown in markdowns:
        # ponytail: two identical items with equal discounts collapse to one
        # vote. Carry the line index if that ever matters.
        for discount in parse_line_discounts(markdown):
            key = (frozenset(_tokens(discount["item_text"])), round(discount["amount"], 2))
            counts[key] += 1
            seen.setdefault(key, discount)
    return [seen[key] for key, count in counts.items() if count >= quorum]


def apply_to_ledger(annotation: dict, markdowns: list[str]) -> bool:
    """Rewrite a reconciled annotation's discount rows as one row per item.

    Runs *after* post-processing rather than before it, on the shape the client
    receives. Attaching earlier changes which candidate consensus selects, and
    the selector ranks on row counts and totals - it has no notion of a discount
    being attached, so a candidate carrying per-item discounts loses to one that
    lumped them into a summary.

    Returns whether anything changed. The caller keeps the result only if the
    receipt still reconciles.
    """
    rows = annotation.get("items") or []
    if not rows:
        return False

    voted = vote_discounts(markdowns)
    if not voted:
        return False

    existing = [r for r in rows if (r.get("category") == "discount")]
    if sorted(round(_to_float(r.get("total")) or 0.0, 2) for r in existing) == sorted(
        round(d["amount"], 2) for d in voted
    ):
        return False  # already the voted set, just possibly in another order

    items = [r for r in rows if r not in existing]
    rebuilt: list[dict] = []
    unplaced = list(voted)
    next_uid = max((r.get("uid") or 0 for r in rows), default=0)
    for item in items:
        rebuilt.append(item)
        name = f"{item.get('name') or ''} {item.get('receipt_line_text') or ''}"
        matches = [d for d in unplaced if _similar(d["item_text"], name) > MATCH_RATIO]
        if matches and not item.get("uid"):
            next_uid += 1
            item["uid"] = next_uid
        for discount in matches:
            unplaced.remove(discount)
            rebuilt.append({
                "name": discount["name"],
                "translated_name": discount["name"],
                "total": f"{discount['amount']:.2f}",
                "category": "discount",
                # The row the UI should show it under, and whose split applies to
                # it. Adjacency already implies it; the id means a reordering
                # client cannot lose the link, and unlike the name it stays
                # unambiguous when a receipt prints the same saving twice.
                "parent_name": item.get("name"),
                "parent_uid": item["uid"],
            })
    if unplaced:
        return False  # a discount with no home; leave the original alone

    annotation["items"] = rebuilt
    total = round(sum(_to_float(r.get("total")) or 0.0 for r in rebuilt), 2)
    annotation["items_total"] = total
    annotation["grand_total"] = round(total + (annotation.get("adjustments_total") or 0), 2)
    return True


def link_orphan_discounts(annotation: dict, markdowns: list[str]) -> int:
    """Give a parent to discount rows that arrived without one. Returns how many.

    A discount the model filed under `other_charges` reaches the client as a bare
    negative row: the right money, attached to nothing. Its amount still appears
    in the OCR under the item it reduces, so the row can be re-attached without
    moving it or changing any total - only naming what it belongs to.

    Rows are matched on amount alone, so an amount appearing twice under
    different items (two identical multibuys) is linked in receipt order.
    """
    rows = annotation.get("items") or []
    orphans = [
        r for r in rows
        if str(r.get("total") or "").startswith("-")
        and not r.get("parent_name")
        and (r.get("category") == "discount" or r.get("kind") == "discount")
    ]
    if not orphans:
        return 0

    available = vote_discounts(markdowns)
    next_uid = max((r.get("uid") or 0 for r in rows), default=0)
    linked = 0
    for row in orphans:
        amount = round(_to_float(row.get("total")) or 0.0, 2)
        match = next((d for d in available if round(d["amount"], 2) == amount), None)
        if match is None:
            continue
        parent = max(
            (r for r in rows if not str(r.get("total") or "").startswith("-")),
            key=lambda r: _similar(
                match["item_text"], f"{r.get('name') or ''} {r.get('receipt_line_text') or ''}"
            ),
            default=None,
        )
        if parent is None:
            continue
        name = f"{parent.get('name') or ''} {parent.get('receipt_line_text') or ''}"
        if _similar(match["item_text"], name) <= MATCH_RATIO:
            continue
        available.remove(match)
        if not parent.get("uid"):
            next_uid += 1
            parent["uid"] = next_uid
        row["parent_name"] = parent.get("name")
        row["parent_uid"] = parent["uid"]
        linked += 1
    return linked


def _to_float(value) -> float | None:
    try:
        return float(str(value).replace("£", "").replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


TESCO = """1 Tesco Universal Bag For Life £0.40
1 Monster Energy Drink Ultra £6.40
4x500ml
Cc £5.25 -£1.15
1 Tesco Finest Dine In Ranch £8.00
Steaks With Peppercorn Melt
303g
Special Offer -£2.40
4 BALANCE DUE £13.35
Savings -£3.55"""

SAINSBURYS = """*CUSHEL Q/TOILT RX12 £13.25
Nectar Price Saving £4.00
TTD OLIVE BREAD 400G £2.10
JS BLUEBERRY MUFINX4 £2.00
Nectar Price Saving £1.00
4 BALANCE DUE £13.35
PROMOTIONS -£5.50"""


def demo():
    tesco = parse_line_discounts(TESCO)
    assert [d["amount"] for d in tesco] == [-1.15, -2.40], tesco
    assert "Monster" in tesco[0]["item_text"], tesco[0]
    # The trailing "Savings" line is past the totals block and must not appear.
    assert len(tesco) == 2, tesco

    sains = parse_line_discounts(SAINSBURYS)
    assert [d["amount"] for d in sains] == [-4.00, -1.00], sains
    assert "CUSHEL" in sains[0]["item_text"], sains[0]

    # A discount only one call in three saw does not survive the vote.
    voted = vote_discounts([SAINSBURYS, SAINSBURYS, TESCO])
    assert sorted(d["amount"] for d in voted) == [-4.00, -1.00], voted

    # One lumped summary row becomes one row per discounted item, adjacent to it
    # and naming its parent, with the bill total unchanged.
    annotation = {
        "items": [
            {"name": "CUSHEL Q/TOILT RX12", "total": "13.25", "category": "item"},
            {"name": "TTD OLIVE BREAD 400G", "total": "2.10", "category": "item"},
            {"name": "JS BLUEBERRY MUFINX4", "total": "2.00", "category": "item"},
            {"name": "PROMOTIONS", "total": "-5.00", "category": "discount"},
        ],
    }
    assert apply_to_ledger(annotation, [SAINSBURYS] * 3) is True
    rows = [(r["name"], r["total"], r.get("parent_name")) for r in annotation["items"]]
    assert rows[1] == ("Nectar Price Saving", "-4.00", "CUSHEL Q/TOILT RX12"), rows
    assert rows[4] == ("Nectar Price Saving", "-1.00", "JS BLUEBERRY MUFINX4"), rows
    assert annotation["items_total"] == 12.35, annotation["items_total"]

    # Each discount binds to its item by id, not by position or name. Two
    # identically-named savings under different items must not collide.
    out = annotation["items"]
    assert out[1]["parent_uid"] == out[0]["uid"], out
    assert out[4]["parent_uid"] == out[3]["uid"], out
    assert out[0]["uid"] != out[3]["uid"], out
    assert out[1]["name"] == out[4]["name"], "the ambiguous case is the point"

    # Rows that already match the voted set are left exactly as they were.
    assert apply_to_ledger(annotation, [SAINSBURYS] * 3) is False

    print(f"ok: tesco {len(tesco)}, sainsburys {len(sains)}, voted {len(voted)}, rows {len(rows)}")


if __name__ == "__main__":
    demo()
