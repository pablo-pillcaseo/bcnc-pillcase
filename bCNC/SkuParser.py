"""
Ikigai Cases / Charmwood Chargers — SKU decomposition parser.

Replaces the enumerated sku_split_map.json with a deterministic grammar.
parse(sku) -> {components: [(component_sku, pct_of_dollars)], product, colours,
               klass, rule, confident}

Design: SKUs are a discrete combinatorial grammar
    <PREFIX>-<PRODUCT>-<COLOUR>[-<COLOUR2>][-<SUFFIX>]
so decomposition is a function of the SKU, not a lookup. Any colour or
colour-pair that Shopify invents parses without a map entry.

Everything a single case rolls up to is the BARE source SKU  SPC-<PRODUCT>-<COLOUR>.
"""

# ---------------------------------------------------------------- vocabulary
COLOURS = {
    'ALUM','AQUA','BAHA','BGSP','BLAC','BRSP','CAMO','CHBL','CNDY','CPPR',
    'DBLU','DESI','DNDR','DSCO','DSRN','EMER','EVBR','FORE','GNML','GOLD',
    'IKIG','INDI','LVDR','MARN','MATT','MKWY','MNGO','NAVY','NYLW','PINK',
    'PSGR','PURP','ROSE','RPTL','RSSP','RWBB','RWBS','SKYB','TEAL','WTCL',
    # Real colours confirmed from ShipHero product names (they were missing from the
    # vocabulary, so both sales AND inventory silently under-resolved them). RVSP is
    # 'Rave Splatter' — almost certainly the live code for what the legacy files
    # called RSSP (kept above; it holds no stock and is harmless).
    'RVSP','DGRN','BPSP',
    # 2026 launch codes, confirmed from ShipHero product names AND the Notion Launch
    # Database (PR38-PR43). Adding a colour never changes dollar allocation — the
    # component key SPC-<P>-<C> is identical whether or not <C> is known — it only
    # flips the parser to 'confident' and gives the code a name. TITA/STST are the two
    # materials of the "Titanium OR Stainless Steel" product launch; they ride the
    # colour slot like any variant.
    'TITA','STST','TIGR','ORPS','SNGR',
    # Not a colour Shopify ever sold. The bucket for units whose colour was
    # genuinely never recorded (see SKU_ALIAS). Naming the gap keeps the units
    # in the product total while refusing to invent a colour for them.
    'UNKN',
}

COLOUR_NAMES = {
    'ALUM':'Aluminum','AQUA':'Acqua Splatter','BAHA':'Bahama Blue',
    'BGSP':'Black+Gold Splatter','BLAC':'Black + Blue Splatter',
    'BRSP':'Black+Red Splatter','CAMO':'Camo Splatter','CHBL':'Cherry Blossom',
    'CNDY':'Cotton Candy','CPPR':'Coffee','DBLU':'Dark Blue','DESI':'Designer Red',
    'DNDR':'Dancing Dragon','DSCO':'Disco Splatter','DSRN':'Desert Rain',
    'EMER':'Emerald Green','FORE':'Forest Green','GNML':'Gunmetal',
    'GOLD':'Golden Rice','IKIG':'Ikigai Orange','INDI':'Indigo','LVDR':'Lavender',
    'MARN':'Maroon','MATT':'Matte Black','MKWY':'Milky Way','MNGO':'Mango',
    'NAVY':'Navy Blue','NYLW':'Mellow Yellow','PINK':'Pink Panther',
    'PURP':'Purple Punch','ROSE':'Rose Gold','RPTL':'Reptile Stripes',
    'RWBB':'Blood Moon','SKYB':'Sky Blue','TEAL':'Teal','WTCL':'Watercolor',
    'RVSP':'Rave Splatter','DGRN':'Dark Green','BPSP':'Black+Pink Splatter',
    'TITA':'Titanium','STST':'Stainless Steel','TIGR':'Tiger Stripes',
    'ORPS':'Ikigai Orange & Purple Splatter','SNGR':'Sunrise Gradient',
    # no confirmed Shopify name — zero sales in 900 days
    'EVBR':None,'PSGR':None,'RSSP':None,'RWBS':None,
    'UNKN':'Colour not recorded',
}

# single-case product codes -> customer-facing title
PRODUCTS = {
    'NPC':'Nano Pill Case','NVC':'Nano Vitamin Case',
    'MPC':'Mission Pill Case','MVC':'Mission Vitamin Case',
    'WPC':'Weekly Pill Case','WVC':'Weekly Vitamin Case',
    'WVXC':'Weekly Vitamin XL Case','WV2XC':'Weekly 2XL Case',
    'WXSPC':'Weekly XS Pill Case',
    # MagNano names read <pockets>P then SIZE — "MagNano 1P Vitamin Case", never
    # "MagNano Vitamin 1P Case". Two orderings of the same three words read as two
    # product lines on a page that lists them together, which is how MGN1V shipped.
    'MGN1P':'MagNano 1P Pill Case','MGN2P':'MagNano 2P Pill Case',
    # MagNano 1P Vitamin — a real new case line (PR39/40/43 launch). Adding it
    # only names/groups the SKU (inventory_title, group_of); it does NOT change
    # decomposition, since parse() keys SPC singles on the COLOUR slot, not PRODUCTS.
    'MGN1V':'MagNano 1P Vitamin Case',
    'WVALS':'Weekly AMPM Vitamin Case - AM Left Side',
    'WVPRS':'Weekly AMPM Vitamin Case - PM Right Side',
    'WVAPLS':'Weekly AMPM Pill Case - AM Left Side',
    'WVPPRS':'Weekly AMPM Pill Case - PM Right Side',
    'WVXALS':'Weekly AMPM Vitamin XL Case - AM Left Side',
    'WVXPRS':'Weekly AMPM Vitamin XL Case - PM Right Side',
    # Magnetic middle piece, sized by the WP / WV / WVX prefix in the SKU.
    'WPMMC':'Magnetic Middle Piece — Pill','WVMMC':'Magnetic Middle Piece — Vitamin',
    'WVXMMC':'Magnetic Middle Piece — Vitamin XL',
}

# Product families, in display order, and WITHIN each family the products in
# canonical order. The single source of truth for how the dashboards group AND order
# products (Group -> Product -> Colour). ENGRAVING has no product codes — it is the
# ENG- prefix — and is grouped by rule in group_of(). Packaging is not a case and is
# grouped separately by the dashboards.
#
# THE ORDER IS THE SIZE LADDER, smallest to largest, because the sizes grow
# sequentially and a customer reads them that way:
#
#     XS  ->  Pill  ->  Vitamin  ->  Vitamin XL  ->  2XL
#
# Weekly is the family that carries all five; every other family is the same ladder
# with the sizes it happens to have. Sorting these by code (WPC, WV2XC, WVC, WVXC,
# WXSPC) or by whatever metric a page happened to default to put 2XL above Vitamin on
# one page and below it on the next — the same drift as four group orders, one level
# down. Read this list top to bottom and it is the ladder.
#
# MagNano has TWO axes and POCKETS is the outer one: all 1-pocket cases (in size
# order), then all 2-pocket, and the 3P/4P/5P lines to come slot in the same way —
# append them to this list in that shape and every page follows.
PRODUCT_GROUPS = [
    ('NANO',                  ['NPC', 'NVC']),
    ('MISSION',               ['MPC', 'MVC']),
    ('WEEKLY',                ['WXSPC', 'WPC', 'WVC', 'WVXC', 'WV2XC']),
    ('WEEKLY AM-PM',          ['WVAPLS', 'WVPPRS', 'WVALS', 'WVPRS', 'WVXALS', 'WVXPRS']),
    ('MAGNANO',               ['MGN1P', 'MGN1V', 'MGN2P']),
    ('MAGNETIC MIDDLE PIECE', ['WPMMC', 'WVMMC', 'WVXMMC']),
    ('ENGRAVING',             []),
]
GROUP_ORDER = [g for g, _ in PRODUCT_GROUPS]
_GROUP_OF = {p: g for g, ps in PRODUCT_GROUPS for p in ps}


def group_of(product_code: str | None) -> str | None:
    """Family a product code belongs to, for the grouped dashboard views. None when
    it is not a grouped case product (packaging, unknown); ENGRAVING is by prefix."""
    if not product_code:
        return None
    pc = product_code.upper()
    if pc in _GROUP_OF:
        return _GROUP_OF[pc]
    if pc.startswith('ENG') or pc.startswith('IG-ENG'):
        return 'ENGRAVING'
    return None


# --- Inventory display families ------------------------------------------------
# The inventory dashboard has to show physical families the SALES grammar never
# emits as a product code: Ultem lids (SPC-<P>-ULTEM-<C> and the bare LID-… part),
# hard cases, MagNano boxes, and cards. family_of() classifies ANY on-hand component
# SKU into a display family; inventory_split() returns the (product, colour) grain
# for the row so the Group -> Product -> Colour tree works on every shape, not just
# SPC-<P>-<C>. Plain cases reuse the 7 PRODUCT_GROUPS via group_of, so the existing
# view is byte-for-byte unchanged — this only adds buckets for the extra families.
PACK_VARIANTS = {'PB': 'Paperbox', 'LB': 'Travel Box'}   # MagNano box, in the colour slot

# Display order for the inventory grouping: the 7 case families first, then the
# physical-only families, then the catch-all. CARDS is defined so the family renders
# the moment the ingest starts capturing them (its exact SKU shape is still TBD).
INVENTORY_FAMILY_ORDER = GROUP_ORDER + ['ULTEM LIDS', 'HARD CASE', 'PACKAGING', 'CARDS', 'OTHER']


def _toks(sku: str) -> list[str]:
    return (sku or '').upper().split('-')


def family_of(component_sku: str | None) -> str:
    """Display family for one inventory component SKU. Total over every physical
    shape (never None — an unrecognised SKU is 'OTHER', which the dashboard still
    shows, so nothing physical is silently dropped)."""
    t = _toks(component_sku)
    if not t or not t[0]:
        return 'OTHER'
    head = t[0]
    if head == 'LID':                                  # bare Ultem lid part, e.g. LID-ULTEM-WPC
        return 'ULTEM LIDS'
    if head == 'PKG':                                  # PKG-<PROD>-PB/-LB box (e.g. PKG-MGN1V-PB)
        return 'PACKAGING'
    if head == 'SPC':
        if 'ULTEM' in t:                               # SPC-<P>-ULTEM-<C> (case + Ultem lid)
            return 'ULTEM LIDS'
        if len(t) >= 3 and t[2] in PACK_VARIANTS:      # SPC-MGN1P-PB / -LB (MagNano box)
            return 'PACKAGING'
        if len(t) >= 2 and t[1] == 'HC':               # SPC-HC-<H>-<C> hard case
            return 'HARD CASE'
        g = group_of(t[1] if len(t) > 1 else None)
        if g:
            return g
    if head == 'BPC' and len(t) >= 2 and t[1] in HARDCASE:   # single-side hard-case listing
        return 'HARD CASE'
    if head in ('CARD', 'CARDS', 'CRD', 'MKT'):        # MKT- is the marketing-card prefix
        return 'CARDS'
    if head.startswith('ENG') or head == 'IG':
        return 'ENGRAVING'
    return 'OTHER'


def inventory_split(component_sku: str | None) -> tuple[str, str]:
    """(product_code, colour_code) for the inventory grain, shape-aware. For a bare
    Ultem lid the 'colour' slot carries the lid marker so the row is distinct from the
    coloured case."""
    t = _toks(component_sku)
    if not t or not t[0]:
        return (component_sku or ''), ''
    if t[0] == 'SPC':
        if 'ULTEM' in t:                               # SPC-<P>-ULTEM-<C> -> product P, colour C
            i = t.index('ULTEM')
            return (t[1] if len(t) > 1 else ''), (t[i + 1] if len(t) > i + 1 else 'ULTEM')
        if len(t) >= 4 and t[1] == 'HC':               # SPC-HC-<H>-<C> -> product H, colour C
            return t[2], t[3]
        return (t[1] if len(t) > 1 else ''), (t[2] if len(t) > 2 else '')
    if t[0] == 'LID':                                  # LID-ULTEM-<P> -> product P, colour 'LID'
        return (t[-1] if len(t) > 1 else 'LID'), 'LID'
    if t[0] == 'PKG':                                  # PKG-<PROD>-<BOXTYPE> -> product, box slot
        return (t[1] if len(t) > 1 else ''), (t[2] if len(t) > 2 else '')
    if t[0] == 'BPC' and len(t) >= 2 and t[1] in HARDCASE:
        return t[1], (t[2] if len(t) > 2 else '')
    return (t[1] if len(t) > 1 else t[0]), (t[2] if len(t) > 2 else '')


# AM-PM bundle code -> (AM/left component product, PM/right component product)
AMPM = {
    'WAC2':     ('WVALS','WVPRS'),      # Weekly AM-PM Vitamin Case 2.0
    'WAPPC2':   ('WVAPLS','WVPPRS'),    # Weekly AM-PM Pill Case 2.0
    'WAPVX2':   ('WVXALS','WVXPRS'),    # Weekly AM-PM Vitamin XL Case 2.0
    '2WVC':     ('WVALS','WVPRS'),      # 2-Week Vitamin Case 2.0
    '2WPC':     ('WVAPLS','WVPPRS'),    # 2-Week Pill Case 2.0
    '2WVXLC2':  ('WVXALS','WVXPRS'),    # 2-Week Vitamin XL Case 2.0
}

# ---- App-wide display conventions (the ONE source; see CLAUDE.md "Display grain") -------
# An AM-PM case is two physical halves — an AM/left side and a PM/right side, each its own
# SPC component — that the customer buys and plans as ONE SET. Every page shows the set, not
# the two halves as separate products: the set's metric is the MIN of its two sides (the
# number of complete sets), and for days-of-stock the set's runway is the smaller side.
# MagNano sells in sets of 7. These live HERE with the rest of the SKU grammar so no page
# re-derives them; nav.inject() ships them to the browser as window.IKIGAI_SETS.
# In the size ladder, same as PRODUCT_GROUPS: Pill -> Vitamin -> Vitamin XL.
AMPM_PAIRS = [
    {'am': 'WVAPLS', 'pm': 'WVPPRS', 'code': 'AMPM-PC',  'name': 'Weekly AM-PM Pill Case'},
    {'am': 'WVALS',  'pm': 'WVPRS',  'code': 'AMPM-VC',  'name': 'Weekly AM-PM Vitamin Case'},
    {'am': 'WVXALS', 'pm': 'WVXPRS', 'code': 'AMPM-VXL', 'name': 'Weekly AM-PM Vitamin XL Case'},
]
# family -> how it is counted as a working "set" a customer buys.
SET_SIZES = {
    'WEEKLY AM-PM': {'size': 2, 'divide': False},   # one 2-piece set = min of the two halves
    'MAGNANO':      {'size': 7, 'divide': True},     # sells in 7s; on-hand shown as sets of 7
}

# The bare component products that are physical HALVES of an AM-PM set, derived from
# AMPM_PAIRS so there is exactly one list of them. Consumers that need "is this an AM-PM
# side" (the inventory breakdown tile, the role classifier) read this instead of pasting
# the six codes again — a second copy is how a new pair would silently miscount. (CLAUDE.md #1)
AMPM_SIDES = frozenset(p['am'] for p in AMPM_PAIRS) | frozenset(p['pm'] for p in AMPM_PAIRS)
# The magnetic-middle-piece products, taken straight from the PRODUCT_GROUPS grammar above.
MMP_PRODUCTS = frozenset(dict(PRODUCT_GROUPS)['MAGNETIC MIDDLE PIECE'])
# The case each middle piece is a PART OF, derived from the code grammar rather than typed:
# the piece is its parent's code with MM inserted before the C (WPC -> WPMMC), so stripping
# 'MMC' and restoring the 'C' recovers the parent. Costing needs this — a part cannot cost
# more to ship than the whole it fits inside, which is what lets 0068 put a measured ceiling
# on a product that has never shipped a run of its own. Derived so that adding a fourth size
# to PRODUCT_GROUPS carries the relationship with it, and so no migration names a SKU (#1).
MMP_PARENT = {p: p[:-3] + 'C' for p in sorted(MMP_PRODUCTS)}


# --- Canonical product display order ------------------------------------------
# The size ladder of PRODUCT_GROUPS, flattened, so ANY page can order a list of
# products the one canonical way with a single lookup — the same treatment the Group
# order (INVENTORY_FAMILY_ORDER) and the set conventions already get, one level down
# (CLAUDE.md #7). nav.inject() ships it to the browser as window.IKIGAI_PRODUCTS.
#
# It is DERIVED, never a second list: reorder PRODUCT_GROUPS and every page moves.
# An AM-PM SET code (AMPM-PC/VC/VXL) ranks where its AM half does, because the set is
# what the pages show and the halves only exist in a drill-down — so a page keyed on
# set codes and a page keyed on component codes still read in the same order.
# A code nobody ranked (Ultem lids, packaging, a listing code) sorts last rather than
# being dropped or silently taking rank 0.
PRODUCT_ORDER: list[str] = [p for _, ps in PRODUCT_GROUPS for p in ps]
_PRODUCT_RANK: dict[str, int] = {p: i for i, p in enumerate(PRODUCT_ORDER)}
for _pair in AMPM_PAIRS:
    _PRODUCT_RANK[_pair['code']] = _PRODUCT_RANK[_pair['am']]
UNRANKED = 9999


def product_rank(code: str | None) -> int:
    """Position of a product (or AM-PM set) code in the canonical size ladder.
    UNRANKED for anything not in the ladder, so it sorts after — never before — the
    products, and never at rank 0 by accident."""
    return _PRODUCT_RANK.get((code or '').upper(), UNRANKED)


# The same ranks, keyed by every name a page might be holding — the product code, the
# AM-PM set code, and the display TITLE — because some grids (the product x colour
# heatmap) carry only the title. Uppercased so the lookup is case-insensitive. This is
# what nav.inject() ships as window.IKIGAI_PRANK; it is derived from the two maps above,
# so it cannot disagree with them.
PRODUCT_RANK_KEYS: dict[str, int] = {k: v for k, v in _PRODUCT_RANK.items()}
PRODUCT_RANK_KEYS.update({PRODUCTS[c].upper(): r for c, r in _PRODUCT_RANK.items()
                          if c in PRODUCTS})
PRODUCT_RANK_KEYS.update({p['name'].upper(): _PRODUCT_RANK[p['code']] for p in AMPM_PAIRS})


# hard-case / half-case listing code -> the single SKU it is physically made of
HARDCASE = {
    'VW2BLC':'WVALS','VW1TRC':'WVPRS','PW2BLC':'WVAPLS','PW1TRC':'WVPPRS',
    'VXL2BLC':'WVXALS','VXL1TRC':'WVXPRS',
    'WVALS':'WVALS','WVPRS':'WVPRS','WVAPLS':'WVAPLS','WVPPRS':'WVPPRS',
    'WVXALS':'WVXALS','WVXPRS':'WVXPRS',
}

# two-DIFFERENT-product combo bundles -> (product A, product B, pct A, pct B).
# Percentages are the retail-value ratio and are carried over verbatim from the
# original hand-built split map.
COMBO = {
    'WPMPCCP':  ('WPC','MPC',0.5595,0.4405),
    'WPMVCCP':  ('WPC','MVC',0.5534,0.4466),
    'WVMPCCP':  ('WVC','MPC',0.5696,0.4304),
    'WVMVCCP':  ('WVC','MVC',0.5537,0.4463),
    'WVWPCCP':  ('WVC','WPC',0.5104,0.4896),
    'WVXWPCCP': ('WVXC','WPC',0.5204,0.4796),
    'WVXWVCCP': ('WVXC','WVC',0.5100,0.4900),
    'WVXMPCCP': ('WVXC','MPC',0.5795,0.4205),
    'WVXMVCCP': ('WVXC','MVC',0.5635,0.4365),
    'MPMVCCP':  ('MVC','MPC',0.5163,0.4837),
    'WPC2':     ('WPC','WPC',0.5000,0.5000),
    'WVC2':     ('WVC','WVC',0.5000,0.5000),
    'WVXC2':    ('WVXC','WVXC',0.5000,0.5000),
    'MPC2':     ('MPC','MPC',0.5000,0.5000),
    'MVC2':     ('MVC','MVC',0.5000,0.5000),
    'NPC2':     ('NPC','NPC',0.5000,0.5000),
    'NVC':      ('NVC','NVC',0.5000,0.5000),
}

PACK = {'2PCB','3PCB','MPCB'}          # n-of-the-same-case bundles
# listing/packaging suffixes that never change which physical SKU was sold
SUFFIX = {'CL','FN','SM','PR','PR2','2P','3P','2L','2','3','LB'}

# Ultem lid part per case product
ULTEM_LID = {
    'NPC':'NPC','NVC':'NVC','MPC':'MPC','MVC':'MVC','WPC':'WPC','WVC':'WVC',
    'WVXC':'WVXC','WV2XC':'WV2XC',
    'WVALS':'WAC2','WVPRS':'WAC2','WVAPLS':'WAPPC2','WVPPRS':'WAPPC2',
    'WVXALS':'WAPVX2','WVXPRS':'WAPVX2',
}
ULTEM_CASE_PCT = 0.7                    # case body / lid revenue split

# Friendly titles for the Ultem-lid part codes that are NOT single-case products in
# PRODUCTS — the AM-PM cases share one lid across both sides (LID-ULTEM-WAC2 etc.), so
# without this the ULTEM LIDS family would show a raw code like "WAC2".
ULTEM_LID_TITLE = {
    'WAC2':   'Weekly AM-PM Vitamin',
    'WAPPC2': 'Weekly AM-PM Pill',
    'WAPVX2': 'Weekly AM-PM Vitamin XL',
}


# Packaging "product" codes that ride the PKG- prefix (PKG-<PROD>-<colour/box>). Not
# cases, so they don't belong in PRODUCTS, but they still need a display name instead
# of a raw code. 2WKV is the 2-Week Weekly (AM-PM) paper-box family.
PKG_PRODUCT_TITLE = {'2WKV': '2-Week Weekly Packaging'}


def inventory_title(product_code: str | None) -> str:
    """Display title for an inventory product code: a single-case product, else an
    Ultem-lid AM-PM code, else a packaging product, else the code itself (never invents
    a name)."""
    pc = (product_code or '').upper()
    return (PRODUCTS.get(pc) or ULTEM_LID_TITLE.get(pc) or PKG_PRODUCT_TITLE.get(pc)
            or product_code or '')


# --- Engraving product resolution ---------------------------------------------
# Engraving is its own grammar riding on the ENG- prefix, NOT a case:
#     [IG-]ENG-<TYPE>-[<PACK>-]<PRODUCT>[-<DETAIL>...]
#   TYPE    LID | IPE | DOTW  (the physical engraving; OPS is an ops check, no case)
#   PACK    2PB | 3PB | ULTEM | MPCB  — sits BEFORE the product; ULTEM also flips
#           the displayed type to "Ultem Lids Engraving" (title_of already does this)
#   PRODUCT the case being engraved: a PRODUCTS code, an AM-PM listing code, or a
#           short legacy code (WXL, MAG, APP, 2WV…). This is what we resolve.
#   DETAIL  a trailing colour / day-of-week / design number — noise for grouping,
#           because the engraving applies to the whole case, not one colour of it.
#
# engraving() resolves PRODUCT to the SAME (family, product_code, title) grain the
# Product Sales table uses, so engravings group Group -> Product exactly like cases
# (the owner's ask: "grouped by product, not by engraving type"). This is the ONLY
# place engraving SKU rules live (non-negotiable #1) — the API tags rows with it and
# the SQL rollup stays a dumb per-SKU sum.
ENG_PACKS = {'2PB', '3PB', 'ULTEM', 'MPCB'}

# Product-slot code -> canonical product code, for the codes that are NOT already a
# PRODUCTS entry or an AM-PM listing code (WAC2/WAPPC2/WAPVX2). Each line is a claim
# about one exact legacy code, read off the engraving SKU list — not a loosening of
# the grammar. An unrecognised product slot stays unresolved (family OTHER), loud by
# omission, never guessed.
ENG_PRODUCT_ALIAS = {
    'WXL':      'WVXC',    # "Weekly XL" listing == Weekly Vitamin XL
    'WVXL':     'WVXC',
    'W2XL':     'WV2XC',   # "Weekly 2XL"
    '2WV':      'WAC2',    # 2-Week Vitamin == AM-PM Vitamin listing
    '2WP':      'WAPPC2',  # 2-Week Pill
    '2WXL':     'WAPVX2',
    '2WVC':     'WAC2',    # the -C listing variants of the same three
    '2WPC':     'WAPPC2',
    '2WVXLC2':  'WAPVX2',
    '2WVXLC':   'WAPVX2',
    'WAC2XL':   'WAPVX2',
    # single hard-case sides engraved on their own -> the AM/PM side product.
    # W2BL is the AM/left side, W1TR the PM/right (mirrors the HARDCASE map).
    'W2BL':     'WVALS',  'W1TR':     'WVPRS',
    '2WVW2BL':  'WVALS',  '2WVW1TR':  'WVPRS',    # 2-week Vitamin sides
    '2WPW2BL':  'WVAPLS', '2WPW1TR':  'WVPPRS',   # 2-week Pill sides
    # day-of-week engravings sold per family, not per exact case
    'MAG':      'MAG',     # MagNano (family-level, no 1P/2P split on the engraving)
    'APP':      'APP',     # Weekly AM-PM (family-level)
    'APXL':     'APXL',    # Weekly AM-PM XL
}

# Titles + families for the resolved codes that are not PRODUCTS entries.
ENG_LISTING_TITLE = {
    'WAC2':   ('Weekly AM-PM Vitamin',    'WEEKLY AM-PM'),
    'WAPPC2': ('Weekly AM-PM Pill',       'WEEKLY AM-PM'),
    'WAPVX2': ('Weekly AM-PM Vitamin XL', 'WEEKLY AM-PM'),
    'APP':    ('Weekly AM-PM',            'WEEKLY AM-PM'),
    'APXL':   ('Weekly AM-PM XL',         'WEEKLY AM-PM'),
    'MAG':    ('MagNano',                 'MAGNANO'),
}


def _eng_family_title(pc: str) -> tuple[str | None, str, str]:
    """(product_code, product_title, family) for a resolved engraving product code.
    OTHER when the slot is not a case we recognise (design number, custom, day code)."""
    if pc in PRODUCTS:
        return pc, PRODUCTS[pc], group_of(pc)
    if pc in ENG_LISTING_TITLE:
        title, fam = ENG_LISTING_TITLE[pc]
        return pc, title, fam
    return None, 'Other engraving', 'OTHER'


def engraving(component_sku: str) -> dict:
    """Resolve an engraving component SKU to the case it engraves. Returns
    {type, product_code, product, family, resolved} — type is the physical
    engraving (Lid / In Pockets / Ultem Lids) from title_of, the rest is the target
    case at Product-Sales grain. resolved=False (family OTHER, product_code None) for
    an ops check, a bare day-of-week, or a design-number slot with no case."""
    t = _toks(component_sku)
    etype = title_of(component_sku) or 'Engraving'
    other = {'type': etype, 'product_code': None, 'product': 'Other engraving',
             'family': 'OTHER', 'resolved': False}
    if t and t[0] == 'IG':                       # IG-ENG-… listing prefix
        t = t[1:]
    if len(t) >= 2 and t[0] == 'BPC' and t[1] == 'ENG':   # engraving riding a bundle
        return {**other, 'type': 'Other engraving'}
    if not t or t[0] != 'ENG' or len(t) < 2:
        return {**other, 'type': 'Other engraving'}
    typ, body = t[1], t[2:]
    if typ == 'OPS':                             # ENG-OPS-CHECK — operational, no case
        return {**other, 'type': 'Other engraving'}
    while body and body[0] in ENG_PACKS:         # peel leading pack/modifier tokens
        body = body[1:]
    if not body:
        return other
    pc = ENG_PRODUCT_ALIAS.get(body[0], body[0])
    code, title, fam = _eng_family_title(pc)
    if code is None:
        return other
    return {'type': etype, 'product_code': code, 'product': title,
            'family': fam, 'resolved': True}

# non-product prefixes -> classification hint used by the sales model
CLASS = {
    'REP':'REP','RED':'REP','ENG':'ENG','ACC':'PKG','MKT':'FREE','PR':'FREE',
    'PLSTC':'FREE','VDS':'FREE','LID':'SALE','IG':'ENG','SPC':'SALE','BPC':'SALE',
    'PCKG':'PKG',
}

# SKUs typed into Shopify wrong, or predating the grammar entirely.
#
# An alias is a claim about ONE exact string, made after reading the drift alert
# that produced it — not a loosening of the grammar. Teaching the grammar to
# accept 'MATTE' as well as 'MATT' would also swallow the next typo in silence,
# and silence is the failure mode this parser exists to prevent. Every entry
# below raised an alert, was looked at, and was understood.
SKU_ALIAS = {
    # 'MATTE' where the colour code is MATT — 3 units, $470.25
    'BPC-WPC2-NYLW-MATTE': 'BPC-WPC2-NYLW-MATT',
    # '3PB' where the pack code is 3PCB — 1 unit, $0.00
    'BPC-3PB-NPC-MATT':    'BPC-3PCB-NPC-MATT',
    # Pre-grammar Mission Pill Case listing, retired before colour codes existed.
    # 225 units, $9,525.10 — the largest single unresolved SKU in the whole
    # history. The colour genuinely was never recorded, so this maps to an
    # explicit unknown rather than to a guess: the units are real and belong in
    # the Mission Pill Case total, but no colour may be inferred from them.
    'MSN-PL-CASE-V1':      'SPC-MPC-UNKN',
}


def _strip_suffixes(tok):
    """Peel trailing listing/packaging suffixes. Returns (core_tokens, suffixes)."""
    suf = []
    while len(tok) > 1 and tok[-1] in SUFFIX:
        suf.insert(0, tok.pop())
    return tok, suf


def parse(sku):
    """Decompose a SKU into (component_sku, pct_of_dollars) pairs summing to 1.0."""
    raw = (sku or '').strip().upper()
    raw = SKU_ALIAS.get(raw, raw)
    out = lambda comps, rule, conf=True, klass=None: {
        'sku': sku, 'components': comps, 'rule': rule, 'confident': conf,
        'colours': [c for c in raw.split('-') if c in COLOURS],
        'klass': klass or CLASS.get(raw.split('-')[0], 'SALE'),
        'product': comps[0][0].split('-')[1] if comps and comps[0][0].count('-') >= 2 else None,
    }
    if not raw:
        return out([], 'empty', False)

    tok = raw.split('-')
    p0 = tok[0]

    # MagNano Travel Box ships under an SPC- prefix but is packaging, not a case
    if raw in ('SPC-MGN2P-LB', 'SPC-MGN1P-LB'):
        return out([(raw, 1.0)], 'packaging.travel_box', klass='PKG')

    # ---- non-product lines: engraving, replacement, packaging, marketing ----
    if p0 in ('ENG', 'REP', 'RED', 'ACC', 'MKT', 'PR', 'PLSTC', 'VDS', 'IG', 'LID',
              'PCKG'):
        return out([(raw, 1.0)], 'passthrough:' + p0)

    # ---- SPC-* singles -------------------------------------------------
    if p0 == 'SPC':
        body, suf = _strip_suffixes(tok[1:])

        # SPC-HC-<listingcode>-<COLOUR>  (hard case / half case listings)
        if body and body[0] == 'HC':
            if len(body) >= 3 and body[-1] in COLOURS and body[1] in HARDCASE:
                return out([(f'SPC-{HARDCASE[body[1]]}-{body[-1]}', 1.0)], 'spc.hardcase')
            return out([(raw, 1.0)], 'spc.hardcase.unknown', False)

        # SPC-<PRODUCT>-ULTEM-<COLOUR>   (case body + Ultem lid, 70/30)
        if len(body) >= 3 and 'ULTEM' in body:
            prod, col = body[0], body[-1]
            if col in COLOURS and prod in ULTEM_LID:
                return out([(f'SPC-{prod}-{col}', ULTEM_CASE_PCT),
                            (f'LID-ULTEM-{ULTEM_LID[prod]}', round(1 - ULTEM_CASE_PCT, 4))],
                           'spc.ultem')
            return out([(raw, 1.0)], 'spc.ultem.unknown', False)

        # SPC-<PRODUCT>-<COLOUR>  (+ any listing suffix already stripped)
        if len(body) == 2 and body[1] in COLOURS:
            return out([(f'SPC-{body[0]}-{body[1]}', 1.0)],
                       'spc.single' + (('+' + '-'.join(suf)) if suf else ''))

        # SPC-<PRODUCT>-<COLOUR1>-<COLOUR2>  (mixed pair sold as one line)
        if len(body) == 3 and body[1] in COLOURS and body[2] in COLOURS:
            return out([(f'SPC-{body[0]}-{body[1]}', 0.5),
                        (f'SPC-{body[0]}-{body[2]}', 0.5)], 'spc.mixed_pair')

        return out([(raw, 1.0)], 'spc.unrecognised', False)

    # ---- BPC-* bundles -------------------------------------------------
    if p0 == 'BPC':
        body, suf = _strip_suffixes(tok[1:])
        if not body:
            return out([(raw, 1.0)], 'bpc.empty', False)
        code = body[0]

        # BPC-{2PCB,3PCB,MPCB}-<PRODUCT>-<COLOUR>[-<COLOUR2>]
        if code in PACK and len(body) >= 3:
            prod = body[1]
            cols = [c for c in body[2:] if c in COLOURS]
            if len(cols) == 1:
                return out([(f'SPC-{prod}-{cols[0]}', 1.0)], 'bpc.pack')
            if len(cols) == 2:      # a 2-pack of two different colours
                return out([(f'SPC-{prod}-{cols[0]}', 0.5),
                            (f'SPC-{prod}-{cols[1]}', 0.5)], 'bpc.pack.mixed')
            return out([(raw, 1.0)], 'bpc.pack.unknown', False)

        # BPC-<AMPMCODE>-<COLOUR1>-<COLOUR2>   AM/left first, PM/right second
        if code in AMPM and len(body) >= 3:
            am, pm = AMPM[code]
            cols = [c for c in body[1:] if c in COLOURS]
            if len(cols) == 2:
                return out([(f'SPC-{am}-{cols[0]}', 0.5), (f'SPC-{pm}-{cols[1]}', 0.5)],
                           'bpc.ampm')
            if len(cols) == 1:
                return out([(f'SPC-{am}-{cols[0]}', 0.5), (f'SPC-{pm}-{cols[0]}', 0.5)],
                           'bpc.ampm.single_colour', False)
            return out([(raw, 1.0)], 'bpc.ampm.unknown', False)

        # BPC-<COMBOCODE>-<COLOUR1>-<COLOUR2>  two different products in one box
        if code in COMBO and len(body) >= 3:
            a, b, pa, pb = COMBO[code]
            cols = [c for c in body[1:] if c in COLOURS]
            if len(cols) == 2:
                return out([(f'SPC-{a}-{cols[0]}', pa), (f'SPC-{b}-{cols[1]}', pb)],
                           'bpc.combo')
            if len(cols) == 1:
                return out([(f'SPC-{a}-{cols[0]}', pa), (f'SPC-{b}-{cols[0]}', pb)],
                           'bpc.combo.single_colour', False)
            return out([(raw, 1.0)], 'bpc.combo.unknown', False)

        # BPC-<SIDE>-<COLOUR>  a single AM/PM side sold as its own bundle line
        if code in HARDCASE and len(body) >= 2 and body[-1] in COLOURS:
            return out([(f'SPC-{HARDCASE[code]}-{body[-1]}', 1.0)], 'bpc.single_side')

        # engraving riding on a bundle, e.g. BPC-ENG-*
        if code == 'ENG':
            return out([(raw, 1.0)], 'bpc.eng', klass='ENG')

        return out([(raw, 1.0)], 'bpc.unrecognised', False)

    return out([(sku, 1.0)], 'unrecognised', False)


def title_of(component_sku):
    """Customer-facing product title for a decomposed component SKU."""
    t = component_sku.split('-')
    if t and t[0] == 'IG':          # IG-ENG-… is an engraving listing prefix
        t = t[1:]
    if t and t[0] == 'SPC' and len(t) >= 2 and t[1] in PRODUCTS:
        return PRODUCTS[t[1]]
    if t and t[0] == 'LID':
        return 'Ultem Lid'
    if t and t[0] in ('REP', 'RED'):
        return 'Replacements'
    if t and t[0] == 'ENG':
        s = '-'.join(t)             # IG- already stripped
        if 'ULTEM' in t:  return 'Ultem Lids Engraving'
        if s.startswith(('ENG-IPE', 'ENG-DOTW')): return 'In Pockets Engraving'
        if s.startswith('ENG-LID'): return 'Lid Engraving'
        return 'In Pockets Engraving'
    if t and t[0] == 'ACC':   return 'Travel Box / Accessory'
    if t and t[0] in ('MKT', 'PR', 'PLSTC', 'VDS'): return 'PR / Marketing Freebie'
    return None


def packaging_title(component_sku):
    """Display title for a PKG-class component (packaging & order inserts). The
    MagNano travel box ships under an SPC- prefix, so title_of would mis-name it as
    the case — handle it first. Never invents: an unrecognised packaging SKU shows
    itself."""
    s = (component_sku or '').upper()
    t = s.split('-')
    if s in ('SPC-MGN2P-LB', 'SPC-MGN1P-LB'):        return 'MagNano Travel Box'
    if len(t) >= 3 and t[0] == 'SPC' and t[2] in PACK_VARIANTS:
        return 'MagNano ' + PACK_VARIANTS[t[2]]      # SPC-MGN*-PB/-LB
    if t and t[0] == 'ACC':                           return 'Travel Box / Accessory'
    if t and t[0] in ('PCKG', 'PKG', 'PKGG'):         return 'Order insert / Packaging'
    return title_of(component_sku) or component_sku


def colour_of(component_sku):
    for x in component_sku.split('-'):
        if x in COLOURS:
            return COLOUR_NAMES.get(x) or x
    return None


# ------------------------------------------------------- colour names, inbound
# The other systems that hold a colour calendar SPELL our colours differently, and
# resolving a spelling is a claim about colour IDENTITY — so it lives here, with the
# grammar, and nowhere else (#1). This map is the whole reason `DKBL` and `DBLU` could
# happen: a second place deciding what a colour name means.
#
# Every entry below was produced by reading a real unresolved row and understanding it,
# never by loosening the match. Three sources feed it:
#
#   * the Notion 🌈 SKU Code Master, which is already keyed on the 4-letter code — its
#     names are aliases only because it spells three of them differently from us;
#   * the Google Sheet colour calendar, which is keyed on a free-text NAME and is
#     therefore the one that can silently drop a colour;
#   * legacy spellings that appear in both.
#
# Matching is on the normalised form (letters and digits, lower-cased), so "Black + Blue
# Splatter" and "Black+Blue Splatter" are the same string and need no entry. Only a
# genuinely DIFFERENT word does.
COLOUR_ALIASES = {
    # the sheet drops the family word our name carries
    'emerald':             'EMER',        # we say Emerald Green
    'camo':                'CAMO',        # we say Camo Splatter
    # a straight misspelling of Acqua, in the sheet's Secret Menu list
    'aquasplatter':        'AQUA',
    # one colour, three names. The sheet calls RWBB by its literal description, we and
    # Notion call it Blood Moon. Nothing about the strings suggests they are the same
    # colour, which is exactly why this entry exists rather than a fuzzy match.
    'redwhiteblackbrush':  'RWBB',
    # the sheet's older name for Mellow Yellow
    'newyellow':           'NYLW',
    # Notion names for codes the parser has never had a confirmed Shopify name for.
    # These are the FIRST names we have for them; `COLOUR_NAMES` still says None because
    # no Shopify listing has ever carried them, and inventing a display name from a
    # planning document is not the same as reading one off a sale.
    'evergladesbrush':     'EVBR',
    'fireicesplatter':     'RWBS',
    'pastelgreen':         'PSGR',
    # Notion writes CPPR as "Copper / Coffee"; both halves resolve
    'copper':              'CPPR',
    'coppercoffee':        'CPPR',
}

# Colour names that appear in a planning source and resolve to NO code, because no such
# code exists — they have never been made, listed or sold. Naming them here is the point:
# an unresolvable name must be a stated fact the calendar shows, never a row that quietly
# vanishes and reads as "this colour was not planned" (#5, #6). Move one out of this set
# and into COLOURS the day it gets a real SKU.
COLOUR_NO_CODE = {
    'truenavyblue',        # sheet: in the Sept–Dec seasonal rotation, but never made
    'patrioticsplatter',   # sheet: "No Plans Yet"
    'rawaluminum',         # sheet: "No Plans Yet"
}


# Colour names whose FINISH is visibly more than one colour. This is a HINT and never an
# answer: it orders the "how many colours is this?" worklist so the obvious cases are not
# buried among forty-odd, and it can never produce a price. The count that a surcharge is
# actually charged on is answered by a person, per colour, and stored on `colour_calendar`
# (0079); a colour flagged here with no count still has NO surcharge, which
# `tests/test_colour_surcharge.py` asserts.
#
# It lives here because it is colour grammar and colour grammar lives in one place (#1), and
# it is a NAME match because the two obvious alternatives are both wrong. The colour CLASS
# cannot answer it — 0067 made class a buy policy, so Disco Splatter and Black+Blue Splatter
# are EVERGREEN and Black+Pink is UNPLANNED, and keying on it would miss five multi-colour
# colourways. And the name cannot answer it either: RWBB is "Blood Moon", which is Red/White/
# Black Brush and contains none of these words — which is exactly why it is listed by CODE
# below rather than left to the pattern.
MULTI_COLOUR_WORDS = (
    'splatter', 'watercolor', 'watercolour', 'gradient', 'ombre', 'marble', 'swirl',
    'camo', 'tie dye', 'tie-dye', 'brush',
)
# Codes whose name gives nothing away. Named individually because a pattern cannot find them.
MULTI_COLOUR_CODES = {
    'RWBB',     # Blood Moon — Red / White / Black Brush
    'CHBL',     # Cherry Blossom
    'CNDY',     # Cotton Candy
    'DNDR',     # Dancing Dragon
    'DSRN',     # Desert Rain
    'SNGR',     # Sunrise Gradient
}


def multi_colour_hint(code: str) -> bool:
    """Does this colour's NAME suggest a multi-colour finish? A hint for a worklist, never a
    price. See MULTI_COLOUR_WORDS."""
    if code in MULTI_COLOUR_CODES:
        return True
    name = (COLOUR_NAMES.get(code) or '').lower()
    return any(w in name for w in MULTI_COLOUR_WORDS)


def _norm_colour(name: str | None) -> str:
    return ''.join(ch for ch in (name or '').lower() if ch.isalnum())


def colour_code_of(name: str | None) -> str | None:
    """Resolve an external colour NAME to our 4-letter code, or None.

    None means exactly one thing: we do not know this colour. It is never a guess and
    never a default — a caller that turns None into a skipped row has reintroduced the
    failure this whole codebase is built around, so callers are expected to surface it.
    `COLOUR_NO_CODE` tells a caller which Nones are *understood* (a real planning entry
    for a colour that does not exist) versus genuinely new.
    """
    n = _norm_colour(name)
    if not n:
        return None
    if n.upper() in COLOURS and len(n) == 4:
        return n.upper()                      # already a code
    hit = COLOUR_ALIASES.get(n)
    if hit:
        return hit
    for code, cname in COLOUR_NAMES.items():
        if cname and _norm_colour(cname) == n:
            return code
    return None


# --------------------------------------------------------- supplier document names
# The factory's packing lists and invoices name a product in ENGLISH (sometimes with the
# Chinese name run onto the end of it), and they do not use our titles. "Weekly XS Case" is
# WXSPC; "MagNano Pill 1P Case小一口磁铁" is MGN1P, whose own title puts the pockets first
# (#7); "AM/PM Pill Case-Left" is one half of the AMPM-PC set.
#
# This is the same shape as COLOUR_ALIASES and it lives here for the same reason (#1): a
# document-name map kept in the freight module would be a second place where a name becomes
# a product, and `DKBL`/`DBLU` is what that costs. Resolution returns the key the freight
# and forecast layers use — a product code, or an AM-PM SET code, since a packing list
# names the halves and everything downstream counts sets.
#
# Matching is on the normalised form (letters and digits only, lower-cased, CJK dropped),
# so punctuation and the trailing Chinese name need no entries of their own.
PRODUCT_DOC_ALIASES = {
    'weeklyxscase':            'WXSPC',      # we say Weekly XS Pill Case
    'weeklyxspillcase':        'WXSPC',
    'magnanopill1pcase':       'MGN1P',      # the list writes size before pockets
    'magnano1ppillcase':       'MGN1P',
    'magnanopill2pcase':       'MGN2P',
    'magnano2ppillcase':       'MGN2P',
    'magnanovitamin1pcase':    'MGN1V',
    'magnano1pvitamincase':    'MGN1V',
    # the AM-PM halves. A packing list files each side as its own line, and it does not
    # spell them the way we do — PR40C files the XL pair as "Weekly AMPM XL Case(left)",
    # with no "Vitamin" in it at all. A missing spelling here is a whole product line
    # dropping out of a load, so both spellings of each side are written down.
    'ampmpillcaseleft':          'WVAPLS',
    'ampmpillcaseright':         'WVPPRS',
    'weeklyampmpillcaseleft':    'WVAPLS',
    'weeklyampmpillcaseright':   'WVPPRS',
    'ampmvitamincaseleft':       'WVALS',
    'ampmvitamincaseright':      'WVPRS',
    'weeklyampmvitamincaseleft': 'WVALS',
    'weeklyampmvitamincaseright':'WVPRS',
    'ampmvitaminxlcaseleft':     'WVXALS',
    'ampmvitaminxlcaseright':    'WVXPRS',
    # the short forms the 2025 lists use
    'ampmvitleft':               'WVALS',
    'ampmvitright':              'WVPRS',
    'ampmxlleft':                'WVXALS',
    'ampmxlright':               'WVXPRS',
    'ampmpillleft':              'WVAPLS',
    'ampmpillright':             'WVPPRS',
    'weeklyampmxlcaseleft':      'WVXALS',
    'weeklyampmxlcaseright':     'WVXPRS',
    'weeklyampmxlcaseleftside':  'WVXALS',
    'weeklyampmxlcaserightside': 'WVXPRS',
    # the magnetic middle pieces, which the factory names after the case they go inside
    'weeklypillmagneticmiddlepiece':      'WPMMC',
    'weeklyvitaminmagneticmiddlepiece':   'WVMMC',
    'weeklyvitaminxlmagneticmiddlepiece': 'WVXMMC',
    # The factory's OWN internal codes, which is how PR38-B files every case: SD<pockets>
    # plus a size. Nothing in the string says which product it is, so each of these was
    # confirmed against a later list that names the product in English — every one matches
    # its pieces per carton, gross weight AND carton dimensions to the gram and the
    # centimetre (SD7-Reg = 40 @ 15.35kg in 49x33x28 = Weekly Pill Case, on both). That is
    # corroboration by an independent document, not a guess from the name.
    'sd1reg':      'NPC',
    'sd1large':    'NVC',
    'sd3reg':      'MPC',
    'sd3large':    'MVC',
    'sd7xs':       'WXSPC',
    'sd7reg':      'WPC',
    'sd7large':    'WVC',
    'sd7xl':       'WVXC',
    'sd72xl':      'WV2XC',
}

# Document lines that resolve to NOTHING on purpose, and why — the same idea as
# COLOUR_NO_CODE and for the same reason: an unresolvable name must be a stated fact, and a
# caller has to be able to tell "understood, and not a product this app buys" from "new, and
# somebody should look". Checked BEFORE the aliases, because several are longer spellings of
# a code that would otherwise match by prefix — "SD1-Large (Titanium)" is not a Nano Vitamin
# Case, it is a different material in a taller carton, and prefix-matching would have priced
# it as one.
PRODUCT_DOC_NO_KEY = {
    'sd1regss316l':    'stainless Nano Pill — a material variant with no SKU here',
    'sd1regtitanium':  'titanium Nano Pill — a material variant with no SKU here',
    'sd1largess316l':  'stainless Nano Vitamin — a material variant with no SKU here',
    'sd1largetitanium':'titanium Nano Vitamin — a material variant with no SKU here',
    '2weekweeklypillpackaging':    'the two-week bundle carton; the order model does not buy it',
    '2weekweeklyvitaminpackaging': 'the two-week bundle carton; the order model does not buy it',
    'magnanovitamin1pleatherbox':  'the leather box is its own quoted SKU, not the paper one',
    'magnanopill1pleatherbox':     'the leather box is its own quoted SKU, not the paper one',
    'magnanopill2pleatherbox':     'the leather box is its own quoted SKU, not the paper one',
    # SD1 with no size qualifier. SD3 and SD7 always carry Regular/Large/XL, and SD1 carries
    # Reg/Large on every list that uses the long form — but PR27 and PR28 both file a bare
    # "SD1", at 75 and 210 to a carton in two different boxes, neither matching Nano Pill's
    # 100 in a 30x32x20. It could be a bulk pack of either Nano. Guessing would put a real
    # carton spec on the wrong product, so it stays unresolved and gets printed.
    'sd1':                         'bare SD1 with no size — ambiguous between the two Nanos',
    # inserts and cards: real cartons on the boat, but the packaging module's stock rather
    # than anything the order calculator buys, so they resolve to no product on purpose
    'cards':                       'insert cards — packaging stock, not an order line',
    'smallpapercard':              'insert cards — packaging stock, not an order line',
    'rectanglepurplebox':          'a retail box with no case named in the line',
    'ampmpillpurplebox':           'a retail box with no case named in the line',
}

# Every product's own title, and every AM-PM half's, resolves without an alias.
PRODUCT_DOC_ALIASES.update({
    ''.join(ch for ch in t.lower() if ch.isalnum()): c for c, t in PRODUCTS.items()})
# ...and so does the SET name, because a document that files a set rather than two halves
# still has to land somewhere.
PRODUCT_DOC_ALIASES.update({
    ''.join(ch for ch in p['name'].lower() if ch.isalnum()): p['code'] for p in AMPM_PAIRS})

# What a packing-list line is, when it is not the case itself. A line is tagged by a word in
# its own description and keyed on the case it belongs to, exactly the way the retail box is:
# "SD7-Regular Ultem lids" is WPC's LID, "MagNano Pill 1P - Paper Box" is MGN1P's PACKAGING.
#
# The order matters. Checked packaging-first, "SD7-Regular Ultem lids" would not match at all
# and — worse — the bare prefix match would then fold it onto the Weekly Pill CASE, pricing
# 228 lids in a 58x40x26 carton as if they were 40 cases in a 49x33x28 one. The importer's
# conflict guard caught exactly that on PR27 and refused to emit either.
PRODUCT_DOC_KINDS = (
    ('ultemlids', 'LID'),
    ('ultemlid',  'LID'),
    ('paperbox',  'PACKAGING'),
    ('packaging', 'PACKAGING'),
)

# What the packing list files as PACKAGING rather than as the product itself: the retail
# paper box, named after the case it holds. It is a separate carton with its own dimensions,
# so it is a separate observation, and conflating the two would put the box's volume onto
# the case (0065's mistake in a new place).
#
# The LEATHER box is deliberately not in here. PR40C ships both a leather and a paper box
# for MGN1V, in different cartons (48 to a 54x47x32 against 40 to a 42x28x38), and they are
# separately quoted SKUs. Folding them into one PACKAGING row would make the answer depend
# on which line the parse read last — so the leather box resolves to nothing and is
# reported, which is the honest shape until something actually orders one.
PRODUCT_DOC_PACKAGING = ('paperbox', 'packaging')


def _norm_doc(name: str | None) -> str:
    return ''.join(ch for ch in (name or '').lower() if ch.isascii() and ch.isalnum())


def _no_key_hit(n: str) -> str | None:
    """The longest deliberately-absent name this line starts with — but only if no ALIAS
    matches it more specifically.

    The specificity test is the whole of it. `sd1` is deliberately absent (bare SD1 is
    ambiguous between the two Nanos) while `sd1large` is Nano Vitamin, and a plain
    startswith on the absent list swallowed the second: SD1-Large stopped resolving the
    moment the bare code was named. Longest match across BOTH lists wins.
    """
    absent = [a for a in PRODUCT_DOC_NO_KEY if n.startswith(a)]
    if not absent:
        return None
    best = max(absent, key=len)
    alias = [a for a in PRODUCT_DOC_ALIASES if n.startswith(a)]
    if alias and len(max(alias, key=len)) > len(best):
        return None
    return best


def product_code_of(name: str | None) -> tuple[str | None, str]:
    """Resolve a supplier document's product NAME to (key, kind).

    `kind` is 'PACKAGING' when the line names the retail box rather than the case, and
    'PRODUCT' otherwise. A key of None means we do not know this name — never a guess and
    never a default, exactly as `colour_code_of` promises. A caller that drops a None has
    reintroduced the failure this codebase is built around; surface it instead.
    """
    n = _norm_doc(name)
    if not n:
        return None, 'PRODUCT'
    kind = 'PRODUCT'
    for tag, k in PRODUCT_DOC_KINDS:
        if tag in n:
            kind = k
            n = n.replace(tag, '')
            break
    # a document often appends the run, a size or the Chinese name; the longest alias that
    # the line STARTS with is the product, which is how "MagNano Pill 1P Case" beats
    # "MagNano Pill 1P" without either needing a rule of its own.
    if _no_key_hit(n) or _no_key_hit(_norm_doc(name)):
        return None, kind
    if n in PRODUCT_DOC_ALIASES:
        return PRODUCT_DOC_ALIASES[n], kind
    # The box line names the case without the word "case" — "MagNano Vitamin 1P - Leather
    # Box" is MGN1V's box — so the word is dropped from BOTH sides rather than a second
    # alias being written for every product.
    m = {a.replace('case', ''): k for a, k in PRODUCT_DOC_ALIASES.items()}
    if n.replace('case', '') in m:
        return m[n.replace('case', '')], kind
    # a document often appends the run, a size or the Chinese name; the longest alias the
    # line STARTS with is the product, which is how "MagNano Pill 1P Case小一口磁铁" resolves
    # without an entry of its own.
    hits = [a for a in PRODUCT_DOC_ALIASES if n.startswith(a)]
    if hits:
        return PRODUCT_DOC_ALIASES[max(hits, key=len)], kind
    return None, kind


# --------------------------------------------------------------------- titles
# Lines with no SKU at all. Shopify's own sales reports exclude gift cards and
# draft-order payments from gross/net sales (verified: neither appears in
# `FROM sales GROUP BY product_title` for July 2026, though both exist in our
# order lines). Counting them would put us permanently above Shopify with no
# way to reconcile, so they are classified out — not dropped. They still get a
# component so every dollar in order_line is accounted for somewhere.
NON_SALE_TITLE_RULES = (
    ("GIFTCARD", ("gift card",)),
    ("PAYMENT",  ("payment for order", "payment for replacement order",
                  "payment for refund", "invoice for original order",
                  "invoice for order", "additional payment")),
)


def classify_title(title: str | None) -> str:
    """Class for a line that carries no SKU, from its product title.

    Returns GIFTCARD / PAYMENT for things Shopify keeps out of sales, or
    UNMAPPED for a real product we cannot yet resolve. UNMAPPED is deliberately
    loud: it is a real gap, not a category.
    """
    t = (title or "").strip().lower()
    if not t:
        return "UNMAPPED"
    for klass, needles in NON_SALE_TITLE_RULES:
        if any(n in t for n in needles):
            return klass
    return "UNMAPPED"


def klass_of(sku: str | None, title: str | None) -> str:
    """The one place a line's class is decided, SKU or not."""
    sku = (sku or "").strip()
    if sku:
        return parse(sku)["klass"]
    return classify_title(title)
