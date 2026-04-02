from . import pokemon_center, walmart, target, ebay

PARSER_MAP = {
    "pokemon_center": pokemon_center.parse,
    "walmart":        walmart.parse,
    "target":         target.parse,
    "ebay":           ebay.parse,
}


def parse_email(email_data):
    retailer  = email_data.get("retailer")
    parser_fn = PARSER_MAP.get(retailer)
    if parser_fn is None:
        return None
    return parser_fn(email_data)