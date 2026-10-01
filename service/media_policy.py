"""Images in a customer's posts and site (spec 28.19): what the owner may publish, decided before a proposal is made.

    verdict, reason = check_image({"source": "stock", "provider": "pexels", "license_url": "...", "depicts_offer": False},
                                  caption="صورة توضيحية · عرض الجمعة")

A stock photo of mandi presented as the restaurant's own dish misleads its customers, whatever the photo's license
says. So:
  owner      the owner's own photo: pass
  stock      Unsplash, Pexels or Pixabay only, the photo's page recorded, the caption says "صورة توضيحية", and never as
             the business's own product (depicts_offer): otherwise blocked
  generated  only from a generator whose commercial license is confirmed in writing (docs/licensing_matrix.md; FLUX.1
             [dev] is non-commercial, P3), with the same label and the same rule on the business's own product
Anything else is blocked. The verdict is a code the owner's proposal shows; nothing here fetches the image.
"""
from __future__ import annotations
from urllib.parse import urlsplit

LABEL = "صورة توضيحية"
STOCK = {"unsplash": "unsplash.com", "pexels": "pexels.com", "pixabay": "pixabay.com"}
COMMERCIAL_GENERATORS: frozenset[str] = frozenset()   # none confirmed in writing yet (P3); add only with the letter


def check_image(image: dict, caption: str = "") -> tuple[str, str]:
    """('pass' | 'block', reason code)."""
    source = image.get("source")
    if source == "owner":
        return "pass", "OWNER_PHOTO"
    if source not in ("stock", "generated"):
        return "block", "UNKNOWN_SOURCE"
    if image.get("depicts_offer") is not False:          # must be stated, and stated false
        return "block", "NOT_THE_BUSINESS_OWN_PRODUCT"
    if LABEL not in (caption or ""):
        return "block", "ILLUSTRATIVE_LABEL_MISSING"
    if source == "stock":
        provider = image.get("provider")
        if provider not in STOCK:
            return "block", "STOCK_PROVIDER_NOT_ALLOWED"
        host = (urlsplit(str(image.get("license_url") or "")).hostname or "").lower()
        if not (host == STOCK[provider] or host.endswith("." + STOCK[provider])):
            return "block", "STOCK_PAGE_NOT_RECORDED"
        return "pass", "STOCK_LABELLED"
    if image.get("generator") not in COMMERCIAL_GENERATORS:
        return "block", "GENERATOR_LICENSE_UNCONFIRMED"
    return "pass", "GENERATED_LABELLED"
