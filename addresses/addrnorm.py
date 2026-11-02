"""Street / place normalization shared by the offline address pack tools.

This module is the reference implementation of the normalization spec in
docs/address-pack.md ("Normalization"). The Android app must produce exactly
the same `norm` for a user's query as this code produces for the data, so:

  * keep every rule a plain token-table lookup or a simple left-to-right pass;
  * no regex lookbehinds, no locale-sensitive behaviour;
  * any change here must bump PACK_FORMAT in build-addresses.py, update
    tools/address-normalize-cases.json, and be mirrored in the Kotlin port.

Only the Python standard library is used.
"""

import unicodedata

# Characters deleted outright (not turned into a separator), so that
# "O'Neil" -> "oneil", "U.S." -> "us", "St." -> "st".
DELETE_CHARS = {"'", "’", "‘", "`", "´", "."}

# Ordinal words -> numeric ordinals ("First" -> "1st").
ORDINAL_WORDS = {
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th",
    "fifth": "5th", "sixth": "6th", "seventh": "7th", "eighth": "8th",
    "ninth": "9th", "tenth": "10th", "eleventh": "11th", "twelfth": "12th",
    "thirteenth": "13th", "fourteenth": "14th", "fifteenth": "15th",
    "sixteenth": "16th", "seventeenth": "17th", "eighteenth": "18th",
    "nineteenth": "19th", "twentieth": "20th", "thirtieth": "30th",
    "fortieth": "40th", "fiftieth": "50th", "sixtieth": "60th",
    "seventieth": "70th", "eightieth": "80th", "ninetieth": "90th",
}

# "Twenty First" / "Twenty-First" -> "21st": a tens word followed by a
# 1st..9th ordinal (after ORDINAL_WORDS mapping) is merged.
TENS_WORDS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

DIRECTIONALS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "n": "n", "s": "s", "e": "e", "w": "w",
    "ne": "ne", "nw": "nw", "se": "se", "sw": "sw",
}

# Street types (USPS Publication 28 style abbreviations) plus a few name
# words people habitually abbreviate. Keys are lowercase input tokens,
# values are the canonical token. Note "st" is BOTH "Street" and "Saint":
# mapping both words to "st" means we never need to decide which one it is.
WORD_MAP = {
    # street types
    "alley": "aly", "aly": "aly",
    "avenue": "ave", "ave": "ave", "av": "ave", "aven": "ave", "avn": "ave",
    "bend": "bnd", "bnd": "bnd",
    "boulevard": "blvd", "blvd": "blvd", "boul": "blvd", "blv": "blvd",
    "bypass": "byp", "byp": "byp", "bypa": "byp",
    "circle": "cir", "cir": "cir", "circ": "cir", "crcl": "cir",
    "court": "ct", "ct": "ct", "crt": "ct",
    "cove": "cv", "cv": "cv",
    "creek": "crk", "crk": "crk",
    "crossing": "xing", "xing": "xing", "crssng": "xing",
    "drive": "dr", "dr": "dr", "drv": "dr",
    "expressway": "expy", "expy": "expy",
    "extension": "ext", "ext": "ext",
    "freeway": "fwy", "fwy": "fwy",
    "heights": "hts", "hts": "hts",
    "highway": "hwy", "hwy": "hwy", "hiway": "hwy", "hway": "hwy",
    "hollow": "holw", "holw": "holw", "hollw": "holw",
    "lane": "ln", "ln": "ln",
    "loop": "loop", "lp": "loop",
    "parkway": "pkwy", "pkwy": "pkwy", "pky": "pkwy", "pkway": "pkwy",
    "pike": "pike", "pk": "pike", "pke": "pike",
    "place": "pl", "pl": "pl",
    "plaza": "plz", "plz": "plz",
    "point": "pt", "pt": "pt",
    "ridge": "rdg", "rdg": "rdg",
    "road": "rd", "rd": "rd",
    "route": "rte", "rte": "rte", "rt": "rte",
    "square": "sq", "sq": "sq",
    "street": "st", "st": "st", "str": "st",
    "terrace": "ter", "ter": "ter", "terr": "ter",
    "trace": "trce", "trce": "trce",
    "trail": "trl", "trl": "trl",
    "turnpike": "tpke", "tpke": "tpke",
    "way": "way", "wy": "way",
    # name words
    "saint": "st",
    "mount": "mt", "mt": "mt",
    "mountain": "mtn", "mtn": "mtn",
    "fort": "ft", "ft": "ft",
    "county": "co",
    "interstate": "i",
    "and": "and",
}

# Canonical street-type tokens (subset of WORD_MAP values). Used for the
# leading-directional rule and for collapsing doubled types ("Rd Road").
STREET_TYPES = {
    "aly", "ave", "bnd", "blvd", "byp", "cir", "ct", "cv", "dr", "expy", "ext",
    "fwy", "hwy", "ln", "loop", "pkwy", "pike", "pl", "plz", "rd", "sq", "st",
    "ter", "trce", "trl", "tpke", "way", "xing",
}

# Route designator tokens (after WORD_MAP). A run of these immediately
# followed by an all-digit token collapses to "hwy <n>":
# "US Hwy 70", "US-70", "SR 70", "TN 70", "State Route 70", "Highway 70",
# "Route 70" -> "hwy 70".
ROUTE_WORDS = {"us", "hwy", "sr", "tn", "state", "rte"}

ORDINAL_SUFFIXES = {"st", "nd", "rd", "th"}


def _is_digits(t):
    return t != "" and all("0" <= c <= "9" for c in t)


def _is_letters(t):
    return t != "" and all("a" <= c <= "z" for c in t)


def tokenize(s):
    """Steps 1-6 of the spec: fold, lowercase, split, split letter/digit runs."""
    if not s:
        return []
    # 1. NFKD + drop combining marks (accents).
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    # 2. lowercase (ASCII-relevant; root locale)
    s = s.lower()
    # 3. delete apostrophes and periods; "&" -> " and "; everything that is
    #    not a-z / 0-9 becomes a space.
    out = []
    for c in s:
        if c in DELETE_CHARS:
            continue
        if c == "&":
            out.append(" and ")
        elif ("a" <= c <= "z") or ("0" <= c <= "9"):
            out.append(c)
        else:
            out.append(" ")
    raw = "".join(out).split()
    # 4. split "us70" -> "us","70"; "19e" -> "19","e" (but keep "1st","22nd").
    tokens = []
    for t in raw:
        i = 0
        while i < len(t) and "a" <= t[i] <= "z":
            i += 1
        if 0 < i < len(t) and _is_digits(t[i:]):
            tokens.append(t[:i])
            tokens.append(t[i:])
            continue
        j = 0
        while j < len(t) and "0" <= t[j] <= "9":
            j += 1
        if 0 < j < len(t) and _is_letters(t[j:]) and t[j:] not in ORDINAL_SUFFIXES:
            tokens.append(t[:j])
            tokens.append(t[j:])
            continue
        tokens.append(t)
    # 5. strip leading zeros from all-digit tokens ("070" -> "70").
    # 6. "u s" -> "us"
    res = []
    for t in tokens:
        if _is_digits(t):
            t = t.lstrip("0") or "0"
        if t == "s" and res and res[-1] == "u":
            res[-1] = "us"
            continue
        res.append(t)
    return res


def map_tokens(tokens):
    """Step 7: per-token table lookups plus 'twenty first' -> '21st'."""
    out = []
    for t in tokens:
        t = ORDINAL_WORDS.get(t, t)
        t = DIRECTIONALS.get(t, t)
        t = WORD_MAP.get(t, t)
        # tens word + 1st..9th
        if out and out[-1] in TENS_WORDS and len(t) == 3 and t[0] in "123456789" \
                and t[1:] in ORDINAL_SUFFIXES:
            out[-1] = str(TENS_WORDS[out[-1]] + int(t[0])) + t[1:]
            continue
        out.append(t)
    return out


def normalize_street(s: str) -> str:
    """Canonical street key. See docs/address-pack.md, 'Normalization'."""
    toks = map_tokens(tokenize(s))
    # 8. route collapse: run of ROUTE_WORDS followed by digits -> "hwy N"
    #    county road: "co rd N" / "co hwy N" / "co rte N" / "cr N" -> "cr N"
    out = []
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t == "co" and i + 2 < n and toks[i + 1] in ("rd", "hwy", "rte") \
                and _is_digits(toks[i + 2]):
            out += ["cr", toks[i + 2]]
            i += 3
            continue
        if t == "cr" and i + 1 < n and _is_digits(toks[i + 1]):
            out += ["cr", toks[i + 1]]
            i += 2
            continue
        if t in ROUTE_WORDS:
            j = i
            while j < n and toks[j] in ROUTE_WORDS:
                j += 1
            if j < n and _is_digits(toks[j]):
                out += ["hwy", toks[j]]
                i = j + 1
                continue
        out.append(t)
        i += 1
    # 9. drop a street type that repeats the previous token ("rd rd").
    dedup = []
    for t in out:
        if dedup and t == dedup[-1] and t in STREET_TYPES:
            continue
        dedup.append(t)
    # 10. move ONE leading directional to the end when what follows contains
    #     at least one token that is not a street type or directional:
    #     "N Main St" -> "main st n", but "North St" stays "n st".
    if len(dedup) >= 2 and dedup[0] in DIRECTIONALS.values():
        rest = dedup[1:]
        if any(t not in STREET_TYPES and t not in DIRECTIONALS.values() for t in rest):
            dedup = rest + [dedup[0]]
    return " ".join(dedup)


def normalize_place(s: str) -> list:
    """Tokens for a city / locality: steps 1-7 only (no reordering)."""
    return map_tokens(tokenize(s))


def normalize_number(s: str) -> str:
    """House number key: lowercase, keep only a-z 0-9 and '/'. '12-B' -> '12b'."""
    if not s:
        return ""
    s = s.lower()
    return "".join(c for c in s if ("a" <= c <= "z") or ("0" <= c <= "9") or c == "/")


# Words dropped from unit designators: "Apt 4" -> "4", "LOT 13" -> "13".
UNIT_WORDS = {"apt", "apartment", "unit", "ste", "suite", "lot", "rm", "room",
              "no", "num", "bldg", "building", "trlr", "spc", "space"}


def normalize_unit(s: str) -> str:
    """Unit key: lowercase, punctuation -> space, drop UNIT_WORDS tokens,
    join the rest with no separator. 'Apt. 4-B' -> '4b', '#12' -> '12'."""
    if not s:
        return ""
    out = []
    for c in s.lower():
        out.append(c if (("a" <= c <= "z") or ("0" <= c <= "9") or c == "/") else " ")
    return "".join(t for t in "".join(out).split() if t not in UNIT_WORDS)


def split_house_number(number: str, unit: str = ""):
    """(number, num, unit) keys for a raw house number + unit.

    The house number keeps only its first whitespace-separated word, and a
    letter suffix glued to the digits is moved to the unit:
      ('8681', '')        -> ('8681', 8681, '')
      ('509 A', '')       -> ('509', 509, 'a')
      ('112B', '')        -> ('112', 112, 'b')
      ('101 1/2', '')     -> ('101', 101, '1/2')
      ('225 LOT', 'LOT 13') -> ('225', 225, '13')
      ('509 B', '102')    -> ('509', 509, 'b102')
    num is -1 when the number has no leading digits.
    """
    parts = (number or "").split()
    main = normalize_number(parts[0]) if parts else ""
    suffix = " ".join(parts[1:])
    j = 0
    while j < len(main) and "0" <= main[j] <= "9":
        j += 1
    if 0 < j < len(main) and all("a" <= c <= "z" for c in main[j:]):
        suffix = main[j:] + " " + suffix
        main = main[:j]
    sfx = normalize_unit(suffix)
    u = normalize_unit(unit)
    if sfx and u and u != sfx and not u.startswith(sfx):
        u = sfx + u
    elif not u:
        u = sfx
    num = leading_int(main)
    return main, (-1 if num is None else num), u


def leading_int(s: str):
    """Leading digits of a house number as int, or None."""
    j = 0
    while j < len(s) and "0" <= s[j] <= "9":
        j += 1
    return int(s[:j]) if j else None
