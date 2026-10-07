"""hi_res_image_url(): every Creators writer must store the same URL string
for the same photo, or URL-keyed consumers (article heroes, stored comparison
images) see a new image on each refresh (specpicks 2026-10-07)."""
from framework.core.amazon_creators import hi_res_image_url

I = "https://m.media-amazon.com/images/I/"


def test_api_large_rendition_becomes_sl1500():
    assert hi_res_image_url(I + "41pEgUQ8LXL._SL500_.jpg") == I + "41pEgUQ8LXL._AC_SL1500_.jpg"


def test_thumbnails_and_bare_ids_keep_their_id():
    assert hi_res_image_url(I + "31f5pJV++hL._AC_US40_.jpg") == I + "31f5pJV++hL._AC_SL1500_.jpg"
    assert hi_res_image_url(I + "41dtB4o%2BB%2BL.jpg") == I + "41dtB4o%2BB%2BL._AC_SL1500_.jpg"


def test_idempotent():
    u = I + "711aQ7oxvhL._AC_SL1500_.jpg"
    assert hi_res_image_url(u) == u
    assert hi_res_image_url(hi_res_image_url(I + "41HCfKOqCsL._SL500_.jpg")) == I + "41HCfKOqCsL._AC_SL1500_.jpg"


def test_non_amazon_and_empty_pass_through():
    ebay = "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"
    assert hi_res_image_url(ebay) == ebay
    assert hi_res_image_url(None) is None
    assert hi_res_image_url("") == ""
