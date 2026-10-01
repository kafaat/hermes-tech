"""Images in posts: the owner's own photo as is; a stock or generated image only with the "صورة توضيحية" label, a
recorded licensed source, and never as the business's own product."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from service.media_policy import COMMERCIAL_GENERATORS, check_image  # noqa: E402

STOCK = {"source": "stock", "provider": "pexels", "license_url": "https://www.pexels.com/photo/mandi-123/", "depicts_offer": False}
LABELLED = "صورة توضيحية · عروض نهاية الأسبوع"


class TestImages(unittest.TestCase):
    def test_the_owner_photo_passes(self):
        self.assertEqual(check_image({"source": "owner"}), ("pass", "OWNER_PHOTO"))

    def test_a_labelled_stock_photo_from_a_recorded_page_passes(self):
        self.assertEqual(check_image(STOCK, LABELLED), ("pass", "STOCK_LABELLED"))

    def test_a_stock_photo_presented_as_the_dish_is_blocked_whatever_its_license(self):
        for image in ({**STOCK, "depicts_offer": True}, {k: v for k, v in STOCK.items() if k != "depicts_offer"}):
            self.assertEqual(check_image(image, LABELLED), ("block", "NOT_THE_BUSINESS_OWN_PRODUCT"))

    def test_missing_label_provider_or_page_is_blocked(self):
        self.assertEqual(check_image(STOCK, "عروض نهاية الأسبوع")[1], "ILLUSTRATIVE_LABEL_MISSING")
        self.assertEqual(check_image({**STOCK, "provider": "google-images"}, LABELLED)[1], "STOCK_PROVIDER_NOT_ALLOWED")
        for url in ("", "https://pexels.com.evil.example/x", "https://images.example/pexels.com/x"):
            self.assertEqual(check_image({**STOCK, "license_url": url}, LABELLED)[1], "STOCK_PAGE_NOT_RECORDED", url)

    def test_generated_images_wait_for_a_confirmed_commercial_license(self):
        self.assertEqual(COMMERCIAL_GENERATORS, frozenset())
        image = {"source": "generated", "generator": "flux-1-dev", "depicts_offer": False}
        self.assertEqual(check_image(image, LABELLED), ("block", "GENERATOR_LICENSE_UNCONFIRMED"))
        self.assertEqual(check_image({"source": "screenshot"}, LABELLED), ("block", "UNKNOWN_SOURCE"))


if __name__ == "__main__":
    unittest.main()
