"""ShipHero order line items -> the physical lids an operator engraves.

The SKU is the source of truth. `SkuParser.parse()` decomposes it into components,
one per physical case, each of which is a bare `SPC-<PRODUCT>-<COLOUR>` — so a
bundle line like `BPC-WAC2-NAVY-GOLD` resolves to two lids in two colours rather
than to one ambiguous row. `product_name` is never scanned for a colour: it is a
marketing string, and matching substrings of it against thumbnail filenames is what
produced "MULTIPLE COLORS - VERIFY".

resolve_line_item(node) -> [row], one row per engraving text (which is one row per
physical lid, since a lid carries one engraving). Each row is a dict:

    product_name  ShipHero's own title, kept for reference only
    sku           the raw SKU as scanned
    product_code  parser product code, e.g. 'WVC'
    case_type     its customer-facing title, e.g. 'Weekly Vitamin Case'
    colour_code   parser colour code, e.g. 'NAVY'
    colour_name   its display name, e.g. 'Navy Blue' — also the thumbnail key
    engraving     the text to engrave, '' when the line has none
    rule          the parser rule that fired, for diagnosing a bad row
    confident     False when the parser could not fully resolve the SKU
    warnings      [str] — anything the operator must eyeball before cutting

Nothing here guesses. An unresolvable colour or product leaves the field blank and
adds a warning, because a silently wrong colour engraves the wrong lid.
"""

import os
import re

import SkuParser

try:
    from Utils import _
except Exception:  # standalone (tests, a REPL): no Tk, no translations
    def _(s):
        return s


def _ampm_middle_pieces():
    """AM-PM set code -> the magnetic middle piece of the same size.

    Derived from the SKU grammar, not typed, so a new size carries over on its own.
    Each middle piece is its size's case code with MM before the C (WVC -> WVMMC,
    SkuParser.MMP_PARENT), and the AM-PM set of that size is the same case's title
    with "AM-PM" added: 'Weekly Vitamin Case' -> 'Weekly AM-PM Vitamin Case'.
    """
    set_by_title = {p["name"].replace("AM-PM ", "").upper(): p["code"]
                    for p in SkuParser.AMPM_PAIRS}
    out = {}
    for piece, parent in SkuParser.MMP_PARENT.items():
        set_code = set_by_title.get(SkuParser.PRODUCTS.get(parent, "").upper())
        if set_code:
            out[set_code] = piece
    return out


# An AM-PM lid is configured once, as the SET, but it covers three physical
# pieces: the AM and PM halves a SKU decomposes to, AND the magnetic middle piece
# that joins them. The design package's center assemblies (Assy-SD7-MAGSC,
# Assy-SD7-MAGC) use the very same lid part as the sides, so a lid claiming
# 'AMPM-VC' has to answer for WVALS, WVPRS and WVMMC - and likewise every other
# size's middle piece for its own set.
#
# The middle piece is kept out of SkuParser.AMPM_PAIRS on purpose: that grammar is
# shared with the dashboards, where a set is counted as its two halves.
AMPM_MIDDLE_PIECE = _ampm_middle_pieces()
_AMPM_SET_PIECES = {
    p["code"]: tuple(c for c in (p["am"], p["pm"], AMPM_MIDDLE_PIECE.get(p["code"])) if c)
    for p in SkuParser.AMPM_PAIRS}


# Line-item classes that are never a lid under the laser, so they never become a
# row. All three are real order lines; none is something an operator engraves.
#
#   PKG   packaging — a box, not a case.
#   FREE  marketing freebies (MKT-SECRETMENU, MKT-MATCHBOX). They used to yield a
#         row with no case, no colour and no warning, which an operator cannot act
#         on and cannot explain.
#   ENG   the engraving SERVICE sku, which is billing, not an item. A ShipHero
#         AM-PM bundle ships four lines sharing one `_ikg_bundle` id — `:pocket`,
#         `:lid`, `:am_case`, `:pm_case` — and the `:lid` line repeats the engraving
#         text of BOTH halves. Treating it as a case turned two physical lids into
#         four rows, two of them colourless, on every AM-PM 2.0 order. The text is
#         never lost by dropping it: it is always also on the `SPC-` case line that
#         carries the colour. Verified against 16 production totes — every ENG-
#         engraving was duplicated on a real case line, no exceptions.
NON_CASE_KLASSES = frozenset(("PKG", "FREE", "ENG"))


# ------------------------------------------------------------------ colours
def colour_name_of(colour_code):
    """Display name for a colour code. Falls back to the code itself: four codes
    have no confirmed Shopify name, and showing 'EVBR' beats showing nothing."""
    cc = (colour_code or "").strip().upper()
    if not cc:
        return ""
    return SkuParser.COLOUR_NAMES.get(cc) or cc


# --------------------------------------------------------------- thumbnails
# Image types an operator might drop in the thumbnails folder.
THUMBNAIL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")

# Thumbnails filed under a name the parser does not carry. `COLOUR_NAMES` marks
# these codes None because Shopify never confirmed a name for them — but the
# image exists, and an operator holding the lid needs to see it. Naming a file
# here does NOT name the colour in SkuParser, which stays the record of what
# Shopify actually sold.
THUMBNAIL_ALIASES = {
    "PSGR": "Pastel Green",
}


def _thumbnail_key(text):
    """Normalise a colour name or a filename to one comparable key.

    '+' and '&' become 'and' because a display name writes a two-colour finish as
    'Black+Gold Splatter' while the file on disk writes it out as
    `black_and_gold_splatter.png`. Everything else that is not a letter or a
    digit is dropped, so spaces, underscores, hyphens and case never decide
    whether a lid gets its picture — the old exact-basename match is what left
    every '+' colour with no thumbnail at all.
    """
    text = str(text or "").replace("+", " and ").replace("&", " and ")
    return "".join(ch for ch in text.lower() if ch.isalnum())


def thumbnail_basenames(colour_code, colour_name):
    """Candidate thumbnail file basenames (no extension) for a colour, best first.

    The files are named after the display name — 'Black+Gold Splatter' is
    `black_and_gold_splatter.png` — so that convention is what is generated here,
    and the bare code is only a fallback for a colour filed by code. This is the
    name to give a NEW thumbnail; `find_thumbnail` is what reads existing ones,
    and it matches far more loosely than this.
    """
    out = []
    alias = THUMBNAIL_ALIASES.get((colour_code or "").strip().upper())
    for base in (alias, colour_name, colour_code):
        base = str(base or "").strip()
        if not base:
            continue
        base = base.replace("+", " and ").replace("&", " and ").lower()
        slug = "_".join(re.findall(r"[a-z0-9]+", base))
        if slug and slug not in out:
            out.append(slug)
    return out


def thumbnail_dirs(configured=""):
    """Directories to search for colour thumbnails, best first.

    The configured directory always wins, so an operator can point the app at a
    folder of their own. Otherwise the thumbnails that ship with the repo are
    used, which is what makes a fresh `git pull` machine show colours with no
    setup at all. `pillcase_data` is last and legacy: it is gitignored, so it
    only ever held per-machine files, and machines that still have some keep
    working.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        configured,
        os.path.join(here, "color_thumbnails"),
        os.path.join(os.path.dirname(here), "color_thumbnails"),
        os.path.join(here, "pillcase_data", "color_thumbnails"),
    )
    dirs = []
    for d in candidates:
        d = str(d or "").strip()
        if d and d not in dirs:
            dirs.append(d)
    return dirs


_THUMBNAIL_INDEX_CACHE = {}


def _thumbnail_index(thumb_dir):
    """{normalised basename: full path} for one directory, cached on its mtime.

    Cached because the popup asks for an icon per row per redraw, and a colour
    that has no file would otherwise re-stat the whole folder every time.
    """
    try:
        stamp = os.stat(thumb_dir).st_mtime
    except OSError:
        return {}

    cached = _THUMBNAIL_INDEX_CACHE.get(thumb_dir)
    if cached is not None and cached[0] == stamp:
        return cached[1]

    index = {}
    try:
        names = sorted(os.listdir(thumb_dir))
    except OSError:
        return {}
    for fname in names:
        stem, ext = os.path.splitext(fname)
        if ext.lower() not in THUMBNAIL_EXTENSIONS:
            continue
        # setdefault: first file wins, so a folder holding both navy_blue.png and
        # Navy Blue.jpg resolves the same way on every run rather than by chance.
        index.setdefault(_thumbnail_key(stem), os.path.join(thumb_dir, fname))

    _THUMBNAIL_INDEX_CACHE[thumb_dir] = (stamp, index)
    return index


def find_thumbnail(thumb_dirs, colour_code, colour_name):
    """Path to a colour's thumbnail, or None when no directory holds one.

    None is a real answer — a colour whose image has not been shot yet — and the
    caller shows the row without a swatch rather than substituting some other
    colour's picture.
    """
    alias = THUMBNAIL_ALIASES.get((colour_code or "").strip().upper())
    keys = []
    for base in (alias, colour_name, colour_code):
        key = _thumbnail_key(base)
        if key and key not in keys:
            keys.append(key)
    if not keys:
        return None

    if isinstance(thumb_dirs, str):
        thumb_dirs = [thumb_dirs]
    for thumb_dir in thumb_dirs:
        if not thumb_dir:
            continue
        index = _thumbnail_index(thumb_dir)
        for key in keys:
            hit = index.get(key)
            if hit:
                return hit
    return None


# ------------------------------------------------------------ custom options
def engravings_of(custom_options):
    """[(engraving_text, colour_name)] from a line item's custom options.

    ShipHero names each engraving after the case it belongs to — '<Case> Lid
    Engraving' carries the text, and a sibling option named '<Case>' carries the
    colour the customer picked. That colour is what joins an engraving to one of
    the SKU's components when a bundle has more than one.

    Both shapes are handled because the option payload is a dict on some API
    versions and a list on others.
    """
    out = []
    if not custom_options:
        return out

    if isinstance(custom_options, dict):
        for key, value in custom_options.items():
            if not key or not value or "Lid Engraving" not in str(key):
                continue
            base = _engraving_base(key)
            out.append((str(value), _sibling_colour(custom_options, base)))
        return out

    if isinstance(custom_options, list):
        by_name = {}
        for opt in custom_options:
            if isinstance(opt, dict) and opt.get("name"):
                by_name[str(opt["name"])] = opt.get("value") or ""
        for opt in custom_options:
            if not isinstance(opt, dict):
                continue
            name = opt.get("name")
            value = opt.get("value")
            if not name or not value or "Lid Engraving" not in str(name):
                continue
            base = _engraving_base(name)
            out.append((str(value), _sibling_colour(by_name, base)))
    return out


def _engraving_base(option_name):
    """'Weekly Vitamin Case Lid Engraving' -> 'Weekly Vitamin Case'.

    A bare 'Lid Engraving' names no case and reduces to '' - which is the common
    shape in production, not an edge case.
    """
    return str(option_name).replace(" Lid Engraving", "").replace("Lid Engraving", "").strip()


def _sibling_colour(options, base):
    """The colour option named after the case this engraving belongs to.

    '' when the engraving option names no case ('Lid Engraving' on its own): there
    is no sibling to look up, and `options.get("")` asked a question with no
    meaning. The SKU's own colour is the answer in that case, which is what the
    caller falls back to.
    """
    if not base:
        return ""
    return str(options.get(base) or "")


# ----------------------------------------------------------------- the units
def _unit(product_code, colour_code):
    """One physical case: a product and a colour, each named or flagged."""
    pc = (product_code or "").strip().upper()
    cc = (colour_code or "").strip().upper()
    warnings = []

    case_type = SkuParser.inventory_title(pc) if pc else ""
    if pc and pc not in SkuParser.PRODUCTS:
        warnings.append(_("Product code '%s' is not a case the parser knows.") % pc)
    if cc and cc not in SkuParser.COLOURS:
        warnings.append(_("Colour code '%s' is not in the parser vocabulary.") % cc)
        # An unknown code is still shown — as the code — never resolved to a guess.

    return {
        "product_code": pc,
        "case_type": case_type,
        "colour_code": cc,
        "colour_name": colour_name_of(cc),
        "warnings": warnings,
    }


def _units_of(sku, parsed):
    """The physical cases a line item puts in the tote, in SKU order.

    Only bare `SPC-` components are cases. The 30% `LID-ULTEM-*` component of an
    Ultem line is a part of the same case, not a second lid to engrave, and
    packaging is not a case at all.
    """
    if parsed.get("klass") in NON_CASE_KLASSES:
        return []

    units = []
    for comp_sku, _pct in parsed.get("components") or []:
        comp_sku = str(comp_sku)
        if not comp_sku.upper().startswith("SPC-"):
            continue
        if SkuParser.family_of(comp_sku) == "PACKAGING":
            # A MagNano box rides the SPC- prefix but is a box, not a lid.
            continue
        product_code, colour_code = SkuParser.inventory_split(comp_sku)
        units.append(_unit(product_code, colour_code))
    return units


def _take(pool, colour_code):
    """Pop the unit an engraving belongs to; (unit, matched_on_colour).

    Colour is the join key: a two-colour bundle has one engraving per colour, and
    the option's colour is what says which. Without a colour to join on, order is
    the only remaining signal — so it is used, and the caller says so.
    """
    if colour_code:
        for i, unit in enumerate(pool):
            if unit["colour_code"] == colour_code:
                return pool.pop(i), True
    if pool:
        return pool.pop(0), False
    return None, False


# -------------------------------------------------------------------- rows
def resolve_line_item(node):
    """Rows for one ShipHero line item — one per engraving text, else one per case."""
    sku = str(node.get("sku") or "").strip()
    product_name = node.get("product_name") or _("Unknown")
    parsed = SkuParser.parse(sku)

    # Not a lid: contributes nothing to engrave, so it contributes no row. This has
    # to happen before the engraving text is read - an ENG- service line carries
    # text, and taking it would duplicate the case line that carries the same text
    # AND the colour. See NON_CASE_KLASSES.
    if parsed.get("klass") in NON_CASE_KLASSES:
        return []

    units = _units_of(sku, parsed)
    engravings = engravings_of(node.get("custom_options"))

    base = []
    if not sku:
        base.append(_("This line item has no SKU - nothing to match on."))
    elif not parsed.get("confident", True):
        base.append(_("SKU not fully recognised (parser rule: %s).") % parsed.get("rule"))

    def no_case_reason():
        """Why there is no lid here - never a blank row with no reason.

        Packaging and freebies no longer reach this: they return no rows at all.
        What is left is a case SKU the parser could not resolve to a case, which
        is a real problem and says so.
        """
        return _("SKU resolved to no engravable case - set the Lid by hand.")

    def row(unit, engraving, extra):
        unit = unit or _unit("", "")
        return {
            "product_name": product_name,
            "sku": sku,
            "product_code": unit["product_code"],
            "case_type": unit["case_type"],
            "colour_code": unit["colour_code"],
            "colour_name": unit["colour_name"],
            "engraving": engraving,
            "rule": parsed.get("rule", ""),
            "confident": bool(parsed.get("confident", True)),
            "warnings": base + unit["warnings"] + extra,
        }

    if not engravings:
        # No engraving text: still show the cases, so the operator can see what the
        # tote holds. The popup's "hide non-engraving" filter takes them out.
        if units:
            return [row(u, "", []) for u in units]
        # A case SKU that resolved to no case. One row, carrying the reason - a
        # blank row with nothing in it is what an operator cannot act on.
        return [row(None, "", [no_case_reason()])]

    pool = list(units)
    rows = []
    for text, option_colour in engravings:
        extra = []
        option_code = SkuParser.colour_code_of(option_colour) if option_colour else None
        if option_colour and not option_code:
            extra.append(_("Order colour '%s' is not a colour the parser knows.") % option_colour)

        unit, on_colour = _take(pool, option_code)
        if unit is None:
            if units:
                # More engravings than cases: reuse the first rather than drop the
                # engraving, and say the colour is unverified.
                unit = units[0]
                extra.append(_("More engraving lines than this SKU has cases - verify the colour."))
            else:
                extra.append(no_case_reason())
        elif option_code and not on_colour and unit["colour_code"] \
                and option_code != unit["colour_code"]:
            extra.append(_("SKU says %s, the order option says %s - verify before cutting.")
                         % (unit["colour_name"], colour_name_of(option_code)))
        rows.append(row(unit, text, extra))

    # Cases in the tote that no engraving claimed. They are real items the
    # operator is holding, so they are listed rather than dropped - the popup's
    # "hide non-engraving" filter is what decides whether they are shown.
    rows.extend(row(u, "", []) for u in pool)
    return rows


# ------------------------------------------------------------- lid geometry
# The engraving frame is a stored setting, like every other one. It is never
# derived from the lid's NAME: a name is a label an operator reads, and making it
# load-bearing meant every lid had to be called something a regex could parse,
# the size could not be corrected without renaming the lid, and a rename orphaned
# the lid's settings (which are keyed by name).
def lid_dimensions(cfg):
    """(width, length) in mm of a lid's engraving frame, or None when unset.

    Width is the X extent and length the Y extent, which is what the canvas
    outline and the text centring both consume.
    """
    if not isinstance(cfg, dict):
        return None
    try:
        width, length = float(cfg["width"]), float(cfg["length"])
    except (KeyError, TypeError, ValueError):
        return None
    return (width, length) if width > 0 and length > 0 else None


# ------------------------------------------------------- lids <-> products
def configured_product_code(cfg):
    """The product code a lid is configured for, as configured.

    An explicit `productCode` wins. Otherwise the lid's free-text `caseType` is
    resolved through the parser's own document-name resolver, so a lid already
    labelled "Weekly Vitamin Case" (or "Weekly XS Case", or the factory's
    "SD7-Large") keeps working without being re-entered by hand.

    An AM-PM set code is returned as the set, not expanded — that is what the
    settings picker has to show back. Use `lid_product_codes` for matching.
    """
    if not isinstance(cfg, dict):
        return ""
    code = str(cfg.get("productCode") or "").strip().upper()
    if not code:
        resolved, _kind = SkuParser.product_code_of(cfg.get("caseType"))
        code = (resolved or "").upper()
    return code


def lid_product_codes(cfg):
    """Every product code a configured lid covers, for matching a parsed SKU.

    An AM-PM lid is configured once as the SET, but a SKU decomposes to the
    individual pieces — so the set expands here to both halves and the magnetic
    middle piece, which all take the same lid.
    """
    code = configured_product_code(cfg)
    if not code:
        return frozenset()
    if code in _AMPM_SET_PIECES:
        return frozenset(_AMPM_SET_PIECES[code])
    return frozenset([code])


def lids_for_product(lid_defaults, product_code):
    """Every configured lid claiming this product code, in configuration order."""
    pc = (product_code or "").strip().upper()
    if not pc:
        return []
    return [name for name, cfg in (lid_defaults or {}).items()
            if pc in lid_product_codes(cfg)]


def lid_for_product(lid_defaults, product_code):
    """The lid to preselect for a product code, or None when nothing claims it."""
    hits = lids_for_product(lid_defaults, product_code)
    return hits[0] if hits else None


# --------------------------------------------------------- settings choices
def product_choices():
    """'CODE - Title' lines for the lid settings picker, in the size ladder order.

    An AM-PM set ranks where its AM half does, so a set sits with its own family
    rather than at the end of the list.
    """
    entries = [(code, SkuParser.inventory_title(code)) for code in SkuParser.PRODUCT_ORDER]
    entries += [(p["code"], p["name"] + _(" (set)")) for p in SkuParser.AMPM_PAIRS]
    entries.sort(key=lambda e: (SkuParser.product_rank(e[0]), e[0]))
    return ["%s - %s" % (code, title) for code, title in entries]


def code_from_choice(choice):
    """'WVC - Weekly Vitamin Case' -> 'WVC'. Accepts a bare code too."""
    return str(choice or "").split(" - ")[0].strip().upper()


def choice_for_code(code):
    """The picker line for a stored product code, or '' when it has none."""
    code = (code or "").strip().upper()
    if not code:
        return ""
    for choice in product_choices():
        if code_from_choice(choice) == code:
            return choice
    return code
