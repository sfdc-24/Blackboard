"""Provider-neutral typed artifact operations used by the Studio reducer."""
from __future__ import annotations

import copy


def apply_ops(root: dict, ops: list) -> dict:
    """Apply the closed Studio patch vocabulary to a copied artifact tree."""
    tree = copy.deepcopy(root)
    index: dict[str, dict] = {}
    parents: dict[str, dict | None] = {}

    def walk(node: dict, parent: dict | None = None) -> None:
        index[node["id"]] = node
        parents[node["id"]] = parent
        for child in node.get("children") or []:
            walk(child, node)

    walk(tree)
    for operation in ops:
        node = index.get(operation["node_id"])
        if node is None:
            continue
        kind = operation["op"]
        if kind == "set_label":
            node["label"] = operation["value"]
        elif kind == "set_detail":
            node["detail"] = operation["value"]
        elif kind == "insert_child":
            node.setdefault("children", []).append(copy.deepcopy(operation["node"]))
            walk(node["children"][-1], node)
        elif kind == "remove" and parents[operation["node_id"]] is not None:
            parent = parents[operation["node_id"]]
            parent["children"] = [
                child for child in parent["children"]
                if child["id"] != operation["node_id"]
            ]
    return tree


__all__ = ["apply_ops"]
