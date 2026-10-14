"""Groups a tenant's active products into Category -> Sub-category for the
POS terminal's collapsible menu view (app.pos.routes.terminal), only used
when the tenant has the "categories" feature on. A product's category/sub-category
are always optional even then (app.models.product.Product) - this is
where that shows up: an uncategorized item, or one with a category but no
sub-category, still needs somewhere to render.
"""

from dataclasses import dataclass, field


@dataclass
class SubCategoryGroup:
    id: int
    name: str
    products: list = field(default_factory=list)


@dataclass
class CategoryGroup:
    id: int
    name: str
    subcategories: list[SubCategoryGroup] = field(default_factory=list)
    # Products in this category with no sub-category of their own -
    # rendered directly under the category, no extra click needed.
    loose_products: list = field(default_factory=list)


def build_category_tree(products) -> tuple[list[CategoryGroup], list]:
    """(category_groups, uncategorized_products) - categories/sub-categories
    sorted by sort_order then name; a deactivated category or
    sub-category (see products.deactivate_category/deactivate_sub_category)
    is treated the same as none at all, so a product pointed at one just
    falls back to Uncategorized/loose rather than disappearing."""
    categories: dict[int, CategoryGroup] = {}
    sub_groups: dict[int, SubCategoryGroup] = {}
    uncategorized = []

    for p in products:
        category = p.category if (p.category_id and p.category and p.category.is_active) else None
        if category is None:
            uncategorized.append(p)
            continue

        cat_group = categories.get(category.id)
        if cat_group is None:
            cat_group = CategoryGroup(id=category.id, name=category.name)
            categories[category.id] = cat_group

        sub_category = (
            p.sub_category if (p.sub_category_id and p.sub_category and p.sub_category.is_active) else None
        )
        if sub_category is None:
            cat_group.loose_products.append(p)
            continue

        sub_group = sub_groups.get(sub_category.id)
        if sub_group is None:
            sub_group = SubCategoryGroup(id=sub_category.id, name=sub_category.name)
            sub_groups[sub_category.id] = sub_group
            cat_group.subcategories.append(sub_group)
        sub_group.products.append(p)

    ordered = sorted(categories.values(), key=lambda g: g.name)
    for cat_group in ordered:
        cat_group.subcategories.sort(key=lambda g: g.name)

    return ordered, uncategorized
