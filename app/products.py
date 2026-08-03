"""Helpers for file-based products (PDFs delivered from disk).

A product is "file-based" when it has a `file` field pointing to a document on
the server (resolved relative to the catalog's directory). File products are
unlimited stock — the same PDF can be sold any number of times — so they skip
the reservation/inventory path used for unique keys.
"""
from __future__ import annotations

import os


def is_file_product(product: dict) -> bool:
    return bool(product.get("file"))


def resolve_path(product: dict, catalog_dir: str) -> str | None:
    """Absolute path to a product's file, or None if it has no file."""
    f = product.get("file")
    if not f:
        return None
    return f if os.path.isabs(f) else os.path.join(catalog_dir, f)


def file_exists(product: dict, catalog_dir: str) -> bool:
    path = resolve_path(product, catalog_dir)
    return bool(path and os.path.isfile(path))


def product_title(product: dict) -> str:
    return product.get("title") or product.get("name") or str(product.get("id", "item"))


def organization(product: dict) -> str:
    return product.get("organization") or product.get("org") or ""


def group_by_org(products: list[dict]) -> dict[str, list[dict]]:
    """Group products by organization, preserving order. '' → ungrouped."""
    groups: dict[str, list[dict]] = {}
    for p in products:
        groups.setdefault(organization(p), []).append(p)
    return groups
