#!/usr/bin/env python3
"""Convert Grafana schema-v2 dashboard resources into the classic dashboard JSON model."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_SCHEMA_VERSION = 42
DEFAULT_PANEL_HEIGHT = 8
DEFAULT_ROW_HEIGHT = 1
CURSOR_SYNC_TO_GRAPH_TOOLTIP = {
    "Off": 0,
    "Crosshair": 1,
    "Tooltip": 2,
}
AUTO_GRID_COLUMN_WIDTHS = {
    "narrow": 6,
    "standard": 8,
    "wide": 12,
}
AUTO_GRID_ROW_HEIGHTS = {
    "short": 5,
    "standard": 8,
    "tall": 12,
}


class ConversionError(RuntimeError):
    """Raised when a dashboard uses schema-v2 features that cannot be downgraded safely."""


class WarningCollector:
    """Collects non-fatal downgrade warnings for display after conversion."""

    def __init__(self) -> None:
        self._messages: List[str] = []

    def add(self, message: str) -> None:
        if message not in self._messages:
            self._messages.append(message)

    def emit(self) -> None:
        for message in self._messages:
            print(f"warning: {message}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a Grafana dashboard resource created with schema-v2 "
            "(apiVersion dashboard.grafana.app/v2beta1) into the classic dashboard JSON model."
        )
    )
    parser.add_argument("input_path", type=Path, help="Path to the schema-v2 dashboard JSON file.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write the converted classic dashboard JSON to this file. Defaults to stdout.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation level for emitted output. Default: 2.",
    )
    parser.add_argument(
        "--schema-version",
        type=int,
        default=DEFAULT_SCHEMA_VERSION,
        help=(
            "Classic dashboard schemaVersion to write when the input does not provide one. "
            f"Default: {DEFAULT_SCHEMA_VERSION}."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat downgrade warnings as errors instead of emitting a best-effort conversion.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ConversionError(f"{path} did not contain a top-level JSON object")
    return data


def convert_dashboard(
    resource: Dict[str, Any],
    *,
    schema_version: int,
    strict: bool = False,
) -> Tuple[Dict[str, Any], WarningCollector]:
    warnings = WarningCollector()
    spec = extract_dashboard_spec(resource)
    metadata = resource.get("metadata", {}) if isinstance(resource.get("metadata"), dict) else {}

    classic: Dict[str, Any] = {
        "annotations": {"list": convert_annotations(spec.get("annotations", []), warnings)},
        "editable": bool(spec.get("editable", True)),
        "fiscalYearStartMonth": int(spec.get("timeSettings", {}).get("fiscalYearStartMonth", 0)),
        "graphTooltip": CURSOR_SYNC_TO_GRAPH_TOOLTIP.get(spec.get("cursorSync", "Off"), 0),
        "links": deepcopy(spec.get("links", [])),
        "panels": [],
        "preload": bool(spec.get("preload", False)),
        "schemaVersion": infer_schema_version(resource, schema_version),
        "tags": deepcopy(spec.get("tags", [])),
        "templating": {"list": convert_variables(spec.get("variables", []), warnings)},
        "time": convert_time_settings(spec.get("timeSettings", {})),
        "timepicker": convert_timepicker(spec.get("timeSettings", {})),
        "timezone": spec.get("timeSettings", {}).get("timezone", "browser"),
        "title": spec.get("title", ""),
        "uid": infer_uid(resource, metadata),
        "version": infer_dashboard_version(resource),
        "weekStart": spec.get("timeSettings", {}).get("weekStart", ""),
    }

    if bool(spec.get("liveNow")):
        classic["liveNow"] = bool(spec.get("liveNow"))

    panels, next_id = convert_layout(
        spec.get("layout", {}),
        elements=spec.get("elements", {}),
        warnings=warnings,
        strict=strict,
        next_panel_id=find_next_panel_id(spec.get("elements", {})),
        y_offset=0,
    )
    classic["panels"] = panels
    classic["__elementsNextId"] = next_id

    prune_empty_values(classic)
    classic.pop("__elementsNextId", None)
    classic["annotations"] = {"list": classic.get("annotations", {}).get("list", [])}
    classic["templating"] = {"list": classic.get("templating", {}).get("list", [])}
    classic.setdefault("links", [])
    classic.setdefault("tags", [])
    classic.setdefault("timepicker", {})

    if strict and warnings._messages:
        raise ConversionError("; ".join(warnings._messages))

    return classic, warnings


def extract_dashboard_spec(resource: Dict[str, Any]) -> Dict[str, Any]:
    if resource.get("apiVersion") == "dashboard.grafana.app/v2beta1":
        spec = resource.get("spec")
        if not isinstance(spec, dict):
            raise ConversionError("schema-v2 dashboard resource is missing a valid spec object")
        return spec

    if "spec" in resource and isinstance(resource["spec"], dict):
        return resource["spec"]

    raise ConversionError(
        "input JSON does not look like a schema-v2 dashboard resource "
        "(expected apiVersion dashboard.grafana.app/v2beta1 with a spec object)"
    )


def infer_schema_version(resource: Dict[str, Any], default: int) -> int:
    spec = resource.get("spec", {})
    if isinstance(spec, dict) and isinstance(spec.get("schemaVersion"), int):
        return spec["schemaVersion"]
    if isinstance(resource.get("schemaVersion"), int):
        return resource["schemaVersion"]
    return default


def infer_uid(resource: Dict[str, Any], metadata: Dict[str, Any]) -> str:
    spec = resource.get("spec", {})
    if isinstance(spec, dict) and isinstance(spec.get("uid"), str):
        return spec["uid"]
    uid = metadata.get("name")
    return uid if isinstance(uid, str) else ""


def infer_dashboard_version(resource: Dict[str, Any]) -> int:
    spec = resource.get("spec", {})
    if isinstance(spec, dict) and isinstance(spec.get("version"), int):
        return spec["version"]
    metadata = resource.get("metadata", {})
    if isinstance(metadata, dict) and isinstance(metadata.get("generation"), int):
        return metadata["generation"]
    return 0


def convert_annotations(annotations: Iterable[Any], warnings: WarningCollector) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for item in annotations:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        spec = item.get("spec", {}) if isinstance(item.get("spec"), dict) else {}
        if kind == "AnnotationQuery":
            annotation = deepcopy(spec)
            query = spec.get("query")
            if is_builtin_grafana_annotation(query):
                annotation["builtIn"] = 1
                annotation["datasource"] = {"type": "grafana", "uid": "-- Grafana --"}
                annotation.pop("query", None)
            else:
                target = convert_data_query(query, warnings)
                if target:
                    annotation["target"] = target
                annotation.pop("query", None)
            annotation["type"] = "dashboard"
            converted.append(annotation)
            continue

        warnings.add(f"annotation kind {kind!r} is not fully supported and was dropped")
    return converted


def convert_time_settings(time_settings: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "from": time_settings.get("from", "now-6h"),
        "to": time_settings.get("to", "now"),
    }


def convert_timepicker(time_settings: Dict[str, Any]) -> Dict[str, Any]:
    timepicker: Dict[str, Any] = {}
    intervals = time_settings.get("autoRefreshIntervals")
    if isinstance(intervals, list):
        timepicker["refresh_intervals"] = deepcopy(intervals)
    refresh = time_settings.get("autoRefresh")
    if isinstance(refresh, str) and refresh:
        timepicker["refresh"] = refresh
    if bool(time_settings.get("hideTimepicker")):
        timepicker["hidden"] = True
    return timepicker


def is_builtin_grafana_annotation(query: Any) -> bool:
    if not isinstance(query, dict):
        return False
    return (
        query.get("group") == "grafana"
        and query.get("kind") == "DataQuery"
        and isinstance(query.get("spec"), dict)
        and not query["spec"]
    )


def convert_variables(variables: Iterable[Any], warnings: WarningCollector) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for item in variables:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        spec = deepcopy(item.get("spec", {})) if isinstance(item.get("spec"), dict) else {}
        variable = convert_variable(kind, spec, warnings)
        if variable is not None:
            converted.append(variable)
    return converted


def convert_variable(
    kind: Any,
    spec: Dict[str, Any],
    warnings: WarningCollector,
) -> Optional[Dict[str, Any]]:
    kind_to_type = {
        "QueryVariable": "query",
        "TextVariable": "textbox",
        "ConstantVariable": "constant",
        "DatasourceVariable": "datasource",
        "IntervalVariable": "interval",
        "CustomVariable": "custom",
        "AdhocVariable": "adhoc",
    }
    classic_type = kind_to_type.get(kind)
    if classic_type is None:
        warnings.add(f"variable kind {kind!r} is not supported and was dropped")
        return None

    variable = deepcopy(spec)
    variable["type"] = classic_type
    variable["hide"] = normalize_variable_hide(spec.get("hide"))

    if "query" in variable:
        variable["query"] = convert_variable_query(spec.get("query"), warnings)
    if "datasource" in variable:
        variable["datasource"] = convert_datasource_ref(variable.get("datasource"))
    if "current" in variable and isinstance(variable["current"], dict):
        variable["current"] = convert_variable_current(variable["current"])

    return variable


def normalize_variable_hide(value: Any) -> int:
    mapping = {
        "dontHide": 0,
        "hideLabel": 1,
        "hideVariable": 2,
    }
    if isinstance(value, int):
        return value
    return mapping.get(value, 0)


def convert_variable_current(current: Dict[str, Any]) -> Dict[str, Any]:
    converted = deepcopy(current)
    selected = converted.get("selected")
    if selected is None:
        converted["selected"] = False
    return converted


def convert_variable_query(query: Any, warnings: WarningCollector) -> Any:
    if isinstance(query, str):
        return query
    if isinstance(query, dict):
        converted = convert_data_query(query, warnings)
        if "expr" in converted:
            return converted["expr"]
        return converted
    return query


def find_next_panel_id(elements: Dict[str, Any]) -> int:
    max_panel_id = 0
    if not isinstance(elements, dict):
        return 1
    for element in elements.values():
        if not isinstance(element, dict):
            continue
        spec = element.get("spec", {})
        if isinstance(spec, dict) and isinstance(spec.get("id"), int):
            max_panel_id = max(max_panel_id, spec["id"])
    return max_panel_id + 1


def convert_layout(
    layout: Dict[str, Any],
    *,
    elements: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
    next_panel_id: int,
    y_offset: int,
) -> Tuple[List[Dict[str, Any]], int]:
    if not isinstance(layout, dict):
        raise ConversionError("dashboard layout is missing or invalid")

    kind = layout.get("kind")
    spec = layout.get("spec", {}) if isinstance(layout.get("spec"), dict) else {}
    if kind == "GridLayout":
        return convert_grid_layout(
            spec,
            elements=elements,
            warnings=warnings,
            strict=strict,
            next_panel_id=next_panel_id,
            y_offset=y_offset,
        )
    if kind == "RowsLayout":
        return convert_rows_layout(
            spec,
            elements=elements,
            warnings=warnings,
            strict=strict,
            next_panel_id=next_panel_id,
            y_offset=y_offset,
        )
    if kind == "TabsLayout":
        warnings.add("tabs are not supported by the classic schema and were downgraded to rows")
        return convert_tabs_layout(
            spec,
            elements=elements,
            warnings=warnings,
            strict=strict,
            next_panel_id=next_panel_id,
            y_offset=y_offset,
        )
    if kind == "AutoGridLayout":
        warnings.add("auto grid layout was approximated into the classic 24-column grid")
        return convert_auto_grid_layout(
            spec,
            elements=elements,
            warnings=warnings,
            strict=strict,
            next_panel_id=next_panel_id,
            y_offset=y_offset,
        )

    raise ConversionError(f"unsupported layout kind {kind!r}")


def convert_grid_layout(
    spec: Dict[str, Any],
    *,
    elements: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
    next_panel_id: int,
    y_offset: int,
) -> Tuple[List[Dict[str, Any]], int]:
    panels: List[Dict[str, Any]] = []
    items = spec.get("items", [])
    if not isinstance(items, list):
        raise ConversionError("GridLayout.spec.items must be a list")

    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        item_spec = item.get("spec", {}) if isinstance(item.get("spec"), dict) else {}
        if kind == "GridLayoutItem":
            panel = convert_grid_layout_item(
                item_spec,
                elements=elements,
                warnings=warnings,
                strict=strict,
                y_offset=y_offset,
            )
            panels.append(panel)
            continue
        if kind == "GridLayoutRow":
            row_panels, next_panel_id = convert_grid_layout_row(
                item_spec,
                elements=elements,
                warnings=warnings,
                strict=strict,
                next_panel_id=next_panel_id,
                y_offset=y_offset,
            )
            panels.extend(row_panels)
            continue
        warnings.add(f"grid layout item kind {kind!r} is not supported and was dropped")

    return sorted_panels(panels), next_panel_id


def convert_grid_layout_item(
    item_spec: Dict[str, Any],
    *,
    elements: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
    y_offset: int,
) -> Dict[str, Any]:
    panel = convert_element_reference(item_spec.get("element"), elements, warnings, strict)
    panel["gridPos"] = {
        "h": int(item_spec.get("height", DEFAULT_PANEL_HEIGHT)),
        "w": int(item_spec.get("width", 24)),
        "x": int(item_spec.get("x", 0)),
        "y": y_offset + int(item_spec.get("y", 0)),
    }
    apply_repeat_options(panel, item_spec.get("repeat"), warnings)
    if item_spec.get("conditionalRendering"):
        warnings.add(
            f"conditional rendering on panel {panel.get('title', '')!r} is not supported by the classic schema"
        )
    return panel


def convert_grid_layout_row(
    row_spec: Dict[str, Any],
    *,
    elements: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
    next_panel_id: int,
    y_offset: int,
) -> Tuple[List[Dict[str, Any]], int]:
    row_y = y_offset + int(row_spec.get("y", 0))
    row_panel = {
        "collapsed": bool(row_spec.get("collapsed", False)),
        "gridPos": {"h": DEFAULT_ROW_HEIGHT, "w": 24, "x": 0, "y": row_y},
        "id": next_panel_id,
        "panels": [],
        "title": row_spec.get("title", ""),
        "type": "row",
    }
    next_panel_id += 1
    apply_repeat_options(row_panel, row_spec.get("repeat"), warnings)

    child_panels: List[Dict[str, Any]] = []
    elements_list = row_spec.get("elements", [])
    if not isinstance(elements_list, list):
        raise ConversionError("GridLayoutRow.spec.elements must be a list")
    for child in elements_list:
        if not isinstance(child, dict):
            continue
        if child.get("kind") != "GridLayoutItem":
            warnings.add(
                f"grid row child kind {child.get('kind')!r} is not supported and was dropped"
            )
            continue
        child_spec = child.get("spec", {}) if isinstance(child.get("spec"), dict) else {}
        panel = convert_grid_layout_item(
            child_spec,
            elements=elements,
            warnings=warnings,
            strict=strict,
            y_offset=row_y + DEFAULT_ROW_HEIGHT,
        )
        child_panels.append(panel)

    if row_panel["collapsed"]:
        row_panel["panels"] = sorted_panels(child_panels)
        return [row_panel], next_panel_id

    return [row_panel, *sorted_panels(child_panels)], next_panel_id


def convert_rows_layout(
    spec: Dict[str, Any],
    *,
    elements: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
    next_panel_id: int,
    y_offset: int,
) -> Tuple[List[Dict[str, Any]], int]:
    panels: List[Dict[str, Any]] = []
    rows = spec.get("rows", [])
    if not isinstance(rows, list):
        raise ConversionError("RowsLayout.spec.rows must be a list")

    current_y = y_offset
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_spec = row.get("spec", {}) if isinstance(row.get("spec"), dict) else {}
        if row_spec.get("conditionalRendering"):
            warnings.add(
                f"conditional rendering on row {row_spec.get('title', '')!r} is not supported"
            )
        row_panel = {
            "collapsed": bool(row_spec.get("collapse", False)),
            "gridPos": {"h": DEFAULT_ROW_HEIGHT, "w": 24, "x": 0, "y": current_y},
            "id": next_panel_id,
            "panels": [],
            "title": row_spec.get("title", ""),
            "type": "row",
        }
        next_panel_id += 1
        apply_repeat_options(row_panel, row_spec.get("repeat"), warnings)

        nested_layout = row_spec.get("layout", {})
        nested_panels, next_panel_id = convert_layout(
            nested_layout,
            elements=elements,
            warnings=warnings,
            strict=strict,
            next_panel_id=next_panel_id,
            y_offset=current_y + DEFAULT_ROW_HEIGHT,
        )
        nested_panels = shift_panels_to_start_at(nested_panels, current_y + DEFAULT_ROW_HEIGHT)

        if row_panel["collapsed"]:
            row_panel["panels"] = nested_panels
            panels.append(row_panel)
            current_y = estimate_bottom_y([row_panel], current_y + DEFAULT_ROW_HEIGHT)
            continue

        panels.append(row_panel)
        panels.extend(nested_panels)
        current_y = estimate_bottom_y(nested_panels, current_y + DEFAULT_ROW_HEIGHT)

    return sorted_panels(panels), next_panel_id


def convert_tabs_layout(
    spec: Dict[str, Any],
    *,
    elements: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
    next_panel_id: int,
    y_offset: int,
) -> Tuple[List[Dict[str, Any]], int]:
    rows_spec = {"rows": []}
    tabs = spec.get("tabs", [])
    if not isinstance(tabs, list):
        raise ConversionError("TabsLayout.spec.tabs must be a list")
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        tab_spec = tab.get("spec", {}) if isinstance(tab.get("spec"), dict) else {}
        rows_spec["rows"].append(
            {
                "kind": "RowsLayoutRow",
                "spec": {
                    "title": tab_spec.get("title", ""),
                    "collapse": False,
                    "layout": tab_spec.get("layout", {}),
                    "repeat": tab_spec.get("repeat"),
                    "conditionalRendering": tab_spec.get("conditionalRendering"),
                },
            }
        )
    return convert_rows_layout(
        rows_spec,
        elements=elements,
        warnings=warnings,
        strict=strict,
        next_panel_id=next_panel_id,
        y_offset=y_offset,
    )


def convert_auto_grid_layout(
    spec: Dict[str, Any],
    *,
    elements: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
    next_panel_id: int,
    y_offset: int,
) -> Tuple[List[Dict[str, Any]], int]:
    max_column_count = int(spec.get("maxColumnCount", 3) or 3)
    if max_column_count <= 0:
        raise ConversionError("AutoGridLayout.maxColumnCount must be greater than zero")

    width = infer_auto_grid_width(spec, max_column_count)
    height = infer_auto_grid_height(spec)
    panels: List[Dict[str, Any]] = []
    items = spec.get("items", [])
    if not isinstance(items, list):
        raise ConversionError("AutoGridLayout.spec.items must be a list")

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("kind") != "AutoGridLayoutItem":
            warnings.add(
                f"auto grid item kind {item.get('kind')!r} is not supported and was dropped"
            )
            continue
        item_spec = item.get("spec", {}) if isinstance(item.get("spec"), dict) else {}
        panel = convert_element_reference(item_spec.get("element"), elements, warnings, strict)
        row = index // max_column_count
        col = index % max_column_count
        panel["gridPos"] = {"h": height, "w": width, "x": col * width, "y": y_offset + row * height}
        apply_repeat_options(panel, item_spec.get("repeat"), warnings)
        if item_spec.get("conditionalRendering"):
            warnings.add(
                f"conditional rendering on panel {panel.get('title', '')!r} is not supported by the classic schema"
            )
        panels.append(panel)

    return sorted_panels(panels), next_panel_id


def infer_auto_grid_width(spec: Dict[str, Any], max_column_count: int) -> int:
    if spec.get("columnWidthMode") == "custom" and isinstance(spec.get("columnWidth"), (int, float)):
        value = int(spec["columnWidth"])
        return max(1, min(24, value))
    if spec.get("columnWidthMode") in AUTO_GRID_COLUMN_WIDTHS:
        return AUTO_GRID_COLUMN_WIDTHS[str(spec["columnWidthMode"])]
    return max(1, 24 // max_column_count)


def infer_auto_grid_height(spec: Dict[str, Any]) -> int:
    if spec.get("rowHeightMode") == "custom" and isinstance(spec.get("rowHeight"), (int, float)):
        value = int(spec["rowHeight"])
        return max(1, value)
    if spec.get("rowHeightMode") in AUTO_GRID_ROW_HEIGHTS:
        return AUTO_GRID_ROW_HEIGHTS[str(spec["rowHeightMode"])]
    return DEFAULT_PANEL_HEIGHT


def convert_element_reference(
    reference: Any,
    elements: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
) -> Dict[str, Any]:
    if not isinstance(reference, dict):
        raise ConversionError("layout item is missing an element reference")

    name = reference.get("name")
    if not isinstance(name, str):
        raise ConversionError("element reference is missing its name")

    if not isinstance(elements, dict) or name not in elements:
        raise ConversionError(f"element reference {name!r} was not found in dashboard.spec.elements")

    return convert_element(elements[name], warnings, strict)


def convert_element(
    element: Dict[str, Any],
    warnings: WarningCollector,
    strict: bool,
) -> Dict[str, Any]:
    kind = element.get("kind")
    spec = element.get("spec", {}) if isinstance(element.get("spec"), dict) else {}
    if kind == "Panel":
        return convert_panel(spec, warnings)
    if kind == "LibraryPanel":
        raise ConversionError("LibraryPanel elements are not yet supported by this converter")
    if strict:
        raise ConversionError(f"unsupported element kind {kind!r}")
    warnings.add(f"element kind {kind!r} is not supported and was dropped")
    raise ConversionError(f"unsupported element kind {kind!r}")


def convert_panel(spec: Dict[str, Any], warnings: WarningCollector) -> Dict[str, Any]:
    viz_config = spec.get("vizConfig", {}) if isinstance(spec.get("vizConfig"), dict) else {}
    viz_spec = viz_config.get("spec", {}) if isinstance(viz_config.get("spec"), dict) else {}

    panel: Dict[str, Any] = {
        "fieldConfig": deepcopy(viz_spec.get("fieldConfig", {"defaults": {}, "overrides": []})),
        "id": int(spec.get("id", 0)),
        "links": deepcopy(spec.get("links", [])),
        "options": deepcopy(viz_spec.get("options", {})),
        "title": spec.get("title", ""),
        "type": viz_config.get("group") or "timeseries",
    }

    description = spec.get("description")
    if isinstance(description, str) and description:
        panel["description"] = description

    plugin_version = viz_config.get("version")
    if isinstance(plugin_version, str) and plugin_version:
        panel["pluginVersion"] = plugin_version

    data = spec.get("data", {}) if isinstance(spec.get("data"), dict) else {}
    panel.update(convert_query_group(data, warnings))
    prune_empty_values(panel)
    return panel


def convert_query_group(query_group: Dict[str, Any], warnings: WarningCollector) -> Dict[str, Any]:
    if query_group.get("kind") != "QueryGroup":
        warnings.add(f"panel data kind {query_group.get('kind')!r} was preserved best-effort")
    spec = query_group.get("spec", {}) if isinstance(query_group.get("spec"), dict) else {}

    result: Dict[str, Any] = {}
    queries = spec.get("queries", [])
    if isinstance(queries, list):
        targets = []
        datasource = None
        for query in queries:
            target = convert_panel_query(query, warnings)
            if target is None:
                continue
            if datasource is None and isinstance(target.get("datasource"), dict):
                datasource = deepcopy(target["datasource"])
            targets.append(target)
        if targets:
            result["targets"] = targets
        if datasource is not None:
            result["datasource"] = datasource

    transformations = spec.get("transformations")
    if isinstance(transformations, list) and transformations:
        result["transformations"] = deepcopy(transformations)

    query_options = spec.get("queryOptions")
    if isinstance(query_options, dict):
        result.update(convert_query_options(query_options))

    return result


def convert_panel_query(query: Any, warnings: WarningCollector) -> Optional[Dict[str, Any]]:
    if not isinstance(query, dict):
        return None
    kind = query.get("kind")
    spec = query.get("spec", {}) if isinstance(query.get("spec"), dict) else {}
    target = convert_data_query(spec.get("query"), warnings)
    if not target:
        return None
    ref_id = spec.get("refId")
    if isinstance(ref_id, str) and ref_id:
        target["refId"] = ref_id
    hidden = spec.get("hidden")
    if hidden is True:
        target["hide"] = hidden
    datasource = spec.get("datasource")
    if datasource is not None:
        converted_datasource = convert_datasource_ref(datasource)
        if converted_datasource:
            target["datasource"] = converted_datasource
    if kind not in {"PanelQuery", "DataQuery"}:
        warnings.add(f"panel query kind {kind!r} was preserved best-effort")
    return target


def convert_data_query(query: Any, warnings: WarningCollector) -> Dict[str, Any]:
    if query is None:
        return {}
    if isinstance(query, str):
        return {"query": query}
    if not isinstance(query, dict):
        return {}

    spec = query.get("spec", {}) if isinstance(query.get("spec"), dict) else {}
    target = deepcopy(spec)

    group = query.get("group")
    if isinstance(group, str):
        datasource = target.get("datasource")
        if not isinstance(datasource, dict):
            datasource = {}
        datasource.setdefault("type", group)
        target["datasource"] = datasource

    kind = query.get("kind")
    if kind not in {None, "DataQuery"}:
        warnings.add(f"data query kind {kind!r} was preserved best-effort")

    if "datasource" in target:
        converted_datasource = convert_datasource_ref(target["datasource"])
        if converted_datasource:
            target["datasource"] = converted_datasource
        else:
            target.pop("datasource", None)

    return target


def convert_query_options(query_options: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if "cacheTimeout" in query_options:
        result["cacheTimeout"] = query_options["cacheTimeout"]
    if "interval" in query_options:
        result["interval"] = query_options["interval"]
    if "intervalMs" in query_options:
        result["intervalMs"] = query_options["intervalMs"]
    if "maxDataPoints" in query_options:
        result["maxDataPoints"] = query_options["maxDataPoints"]
    if "timeFrom" in query_options:
        result["timeFrom"] = query_options["timeFrom"]
    if "timeShift" in query_options:
        result["timeShift"] = query_options["timeShift"]
    if "queryCachingTTL" in query_options:
        result["queryCachingTTL"] = query_options["queryCachingTTL"]
    return result


def convert_datasource_ref(value: Any) -> Any:
    if isinstance(value, dict):
        datasource: Dict[str, Any] = {}
        uid = value.get("uid")
        ds_type = value.get("type")
        if isinstance(uid, str):
            datasource["uid"] = uid
        if isinstance(ds_type, str):
            datasource["type"] = ds_type
        return datasource or deepcopy(value)
    return deepcopy(value)


def apply_repeat_options(panel: Dict[str, Any], repeat: Any, warnings: WarningCollector) -> None:
    if not isinstance(repeat, dict):
        return
    if repeat.get("mode") != "variable":
        warnings.add(f"repeat mode {repeat.get('mode')!r} is not supported")
        return
    value = repeat.get("value")
    if isinstance(value, str) and value:
        panel["repeat"] = value
    direction = repeat.get("direction")
    if isinstance(direction, str):
        panel["repeatDirection"] = direction
    max_per_row = repeat.get("maxPerRow")
    if isinstance(max_per_row, int):
        panel["maxPerRow"] = max_per_row


def shift_panels_to_start_at(panels: List[Dict[str, Any]], target_y: int) -> List[Dict[str, Any]]:
    if not panels:
        return panels
    min_y = min(panel.get("gridPos", {}).get("y", target_y) for panel in panels)
    delta = target_y - min_y
    if delta == 0:
        return panels
    for panel in panels:
        shift_panel_y(panel, delta)
    return panels


def shift_panel_y(panel: Dict[str, Any], delta: int) -> None:
    if "gridPos" in panel and isinstance(panel["gridPos"], dict):
        panel["gridPos"]["y"] = int(panel["gridPos"].get("y", 0)) + delta
    nested = panel.get("panels")
    if isinstance(nested, list):
        for child in nested:
            if isinstance(child, dict):
                shift_panel_y(child, delta)


def estimate_bottom_y(panels: List[Dict[str, Any]], fallback: int) -> int:
    if not panels:
        return fallback
    bottom = fallback
    for panel in panels:
        grid_pos = panel.get("gridPos", {})
        if isinstance(grid_pos, dict):
            panel_bottom = int(grid_pos.get("y", 0)) + int(grid_pos.get("h", 0))
            bottom = max(bottom, panel_bottom)
        nested = panel.get("panels")
        if isinstance(nested, list) and nested:
            bottom = max(bottom, estimate_bottom_y(nested, fallback))
    return bottom


def sorted_panels(panels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        panels,
        key=lambda panel: (
            int(panel.get("gridPos", {}).get("y", 0)),
            int(panel.get("gridPos", {}).get("x", 0)),
            int(panel.get("id", 0)),
        ),
    )


def prune_empty_values(obj: Any) -> Any:
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            value = prune_empty_values(obj[key])
            if value in ({}, [], "") and key not in {
                "title",
                "description",
                "uid",
                "timezone",
                "weekStart",
                "links",
                "tags",
                "templating",
                "annotations",
                "timepicker",
            }:
                obj.pop(key, None)
            else:
                obj[key] = value
        return obj
    if isinstance(obj, list):
        for item in obj:
            prune_empty_values(item)
        return obj
    return obj


def main() -> int:
    args = parse_args()
    resource = load_json(args.input_path)
    classic, warnings = convert_dashboard(
        resource,
        schema_version=args.schema_version,
        strict=args.strict,
    )

    output_text = json.dumps(classic, indent=args.indent, sort_keys=False) + "\n"
    if args.output is None:
        sys.stdout.write(output_text)
    else:
        args.output.write_text(output_text, encoding="utf-8")

    warnings.emit()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
