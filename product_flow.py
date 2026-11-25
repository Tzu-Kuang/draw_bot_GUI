"""一番くじ官方商品詳情頁入口防護：只允許同商品的「購入する」前往選盒。"""
from __future__ import annotations
from urllib.parse import parse_qs, urlsplit

KUJI_HOST = "on-line.1kuji.com"
PRODUCT_DETAIL_PATH = "/form/product/productdetail.aspx"
SELECT_BOX_PATH = "/form/product/selectbox.aspx"


def product_id(url: str) -> str:
    try:
        params = parse_qs(urlsplit(url).query, keep_blank_values=False)
        return next((v[0] for k, v in params.items() if k.casefold() == "pid" and len(v) == 1), "")
    except ValueError:
        return ""


def matches_product(current_url: str, target_url: str, path: str) -> bool:
    """只允許相同的官方 HTTPS 站點與同一個非空 pid。"""
    try:
        current, target = urlsplit(current_url), urlsplit(target_url)
        pid = product_id(target_url)
        return (current.scheme == target.scheme == 'https'
                and current.hostname == target.hostname == KUJI_HOST
                and current.port in (None, 443) and target.port in (None, 443)
                and current.path.casefold() == path
                and bool(pid) and product_id(current_url) == pid)
    except ValueError:
        return False


def is_product_detail(current_url: str, target_url: str) -> bool:
    return matches_product(current_url, target_url, PRODUCT_DETAIL_PATH)


def is_box_selection(current_url: str, target_url: str) -> bool:
    return matches_product(current_url, target_url, SELECT_BOX_PATH)


def is_product_purchase_label(label: str) -> bool:
    """不接受「注文を確定する」「支払う」等可能帶有金流結果的按鈕。"""
    return ''.join((label or '').split()) == '購入する'
