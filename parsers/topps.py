"""
Topps order email parser — delegates to the generic Shopify parser.
"""

from parsers.shopify_generic import parse as _shopify_parse


def parse(email_data: dict):
    result = _shopify_parse(email_data)
    if result:
        result["retailer"] = "topps"
    return result
