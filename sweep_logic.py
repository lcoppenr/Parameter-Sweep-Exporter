# sweep_logic.py – Core sweep + export logic for ParameterSweepExporter
#
# This module is imported by the add-in entry point.  It owns:
#   • Building the native Fusion command inputs (parameter table, range
#     entries, body/component selection, export format, output folder).
#   • Computing the full combinatorial sweep (range mode) OR
#     a zipped explicit-values sweep (explicit mode).
#   • Driving the timeline, updating parameters, and exporting files.

from __future__ import annotations

import adsk.core
import adsk.fusion
import itertools
import json
import math
import os
import re
import traceback
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Preferences persistence
# ---------------------------------------------------------------------------

_PREFS_PATH = os.path.join(os.path.dirname(__file__), "prefs.json")


def _load_prefs() -> dict:
    """Load saved preferences from disk. Returns {} on any failure."""
    try:
        with open(_PREFS_PATH, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        # Validate: only keep folder if it still exists on disk
        folder = prefs.get("outputFolder", "")
        if folder and not os.path.isdir(folder):
            prefs["outputFolder"] = ""
        return prefs
    except Exception:
        return {}


def _save_prefs(prefs: dict):
    """Write preferences to disk (best-effort, never raises)."""
    try:
        with open(_PREFS_PATH, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Preset persistence
# ---------------------------------------------------------------------------

_PRESETS_PATH = os.path.join(os.path.dirname(__file__), "presets.json")


def _load_presets() -> dict:
    """Load saved presets from disk. Returns {} on any failure."""
    try:
        with open(_PRESETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_presets(presets: dict):
    """Write presets to disk (best-effort, never raises)."""
    try:
        with open(_PRESETS_PATH, "w", encoding="utf-8") as f:
            json.dump(presets, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _float_range(low: float, high: float, steps: int) -> List[float]:
    """Return *steps* evenly-spaced values from *low* to *high* inclusive."""
    if steps < 1:
        return []
    if steps == 1:
        return [low]
    return [low + i * (high - low) / (steps - 1) for i in range(steps)]


def _sanitize(name: str) -> str:
    """Strip characters that are unsafe for file names."""
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def _format_value(val: float) -> str:
    """Compact number string – drop trailing zeros."""
    s = f"{val:.6f}".rstrip("0").rstrip(".")
    return s


def _collect_all_parameters(design: adsk.fusion.Design):
    """Return a list of dicts for favorited parameters in the active design."""
    params = []
    try:
        plist = design.allParameters
        count = plist.count
    except Exception:
        return params

    um = design.unitsManager

    for i in range(count):
        try:
            p = plist.item(i)
            name = p.name
        except Exception:
            continue

        try:
            is_fav = p.isFavorite
        except Exception:
            is_fav = False
        if not is_fav:
            continue

        try:
            expression = p.expression
        except Exception:
            expression = ""
        try:
            raw_value = p.value
        except Exception:
            raw_value = 0.0
        try:
            unit = p.unit
        except Exception:
            unit = ""

        try:
            if unit:
                display_value = um.convert(raw_value, um.internalUnits, unit)
            else:
                display_value = raw_value
        except Exception:
            display_value = raw_value

        try:
            is_user = p.objectType == adsk.fusion.UserParameter.classType()
        except Exception:
            is_user = False

        # Detect text parameters — they have no unit and their value is a string
        try:
            is_text = isinstance(p.value, str) or (unit == "" and isinstance(expression, str) and
                      not any(c.isdigit() for c in expression.replace('"','').replace("'","")))
        except Exception:
            is_text = False
        # More reliable: text params in Fusion have unitType == ""  and value is str
        try:
            is_text = isinstance(p.value, str)
        except Exception:
            pass

        # For text params, use the expression (stripped of quotes) as display value
        if is_text:
            display_value = expression.strip('"').strip("'")

        params.append({
            "name": name,
            "expression": expression,
            "value": display_value,
            "unit": unit,
            "isFavorite": is_fav,
            "isUserParam": is_user,
            "isText": is_text,
        })
    return params


def _collect_all_bodies(design: adsk.fusion.Design):
    """Walk the component tree and return [(body, component, path)] for every
    BRepBody in the design."""
    results = []

    def _walk(occ_path: str, comp: adsk.fusion.Component):
        try:
            body_list = comp.bRepBodies
            for i in range(body_list.count):
                body = body_list.item(i)
                full = f"{occ_path}/{body.name}" if occ_path else body.name
                results.append((body, comp, full))
        except Exception:
            pass
        try:
            occs = comp.occurrences
            for i in range(occs.count):
                occ = occs.item(i)
                child_path = f"{occ_path}/{occ.name}" if occ_path else occ.name
                _walk(child_path, occ.component)
        except Exception:
            pass

    _walk("", design.rootComponent)
    return results


# ---------------------------------------------------------------------------
# Palette / HTML-based command
# ---------------------------------------------------------------------------

PALETTE_ID = "paramSweepPalette"
PALETTE_TITLE = "Parameter Sweep Exporter"
PALETTE_URL = ""
PALETTE_WIDTH = 780
PALETTE_HEIGHT = 760

_palette: adsk.core.Palette = None
_cached_config: dict = None


class _PaletteHTMLHandler(adsk.core.HTMLEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.HTMLEventArgs):
        global _cached_config
        try:
            action = args.action
            data = args.data

            app = adsk.core.Application.get()
            ui = app.userInterface
            design = adsk.fusion.Design.cast(app.activeProduct)

            if action == "ready":
                if not design:
                    args.returnData = json.dumps({
                        "parameters": [],
                        "bodies": [],
                        "error": "No active Fusion design found.",
                    })
                    return

                params = _collect_all_parameters(design)
                bodies = _collect_all_bodies(design)
                prefs = _load_prefs()
                presets = _load_presets()

                payload = json.dumps({
                    "parameters": params,
                    "bodies": [
                        {"path": path, "name": body.name}
                        for body, comp, path in bodies
                    ],
                    "prefs": prefs,
                    "presets": presets,
                })
                args.returnData = payload

            elif action == "browse":
                folder_dlg = ui.createFolderDialog()
                folder_dlg.title = "Select Output Folder"
                result = folder_dlg.showDialog()
                if result == adsk.core.DialogResults.DialogOK:
                    args.returnData = json.dumps({"folder": folder_dlg.folder})
                else:
                    args.returnData = json.dumps({"folder": ""})

            elif action == "save_preset":
                # data = { "name": "...", "preset": { sweepMode, params, ... } }
                payload = json.loads(data)
                preset_name = payload.get("name", "").strip()
                preset_data = payload.get("preset", {})
                if preset_name:
                    presets = _load_presets()
                    presets[preset_name] = preset_data
                    _save_presets(presets)
                    args.returnData = json.dumps({"ok": True, "presets": presets})
                else:
                    args.returnData = json.dumps({"ok": False, "error": "Empty name"})

            elif action == "delete_preset":
                payload = json.loads(data)
                preset_name = payload.get("name", "")
                presets = _load_presets()
                if preset_name in presets:
                    del presets[preset_name]
                    _save_presets(presets)
                args.returnData = json.dumps({"ok": True, "presets": presets})

            elif action == "submit":
                _cached_config = json.loads(data)

                _save_prefs({
                    "exportFormat": _cached_config.get("format", "STEP"),
                    "outputFolder": _cached_config.get("outputFolder", ""),
                    "sweepMode":    _cached_config.get("sweepMode", "range"),
                })

                palette = ui.palettes.itemById(PALETTE_ID)
                if palette:
                    palette.isVisible = False
                _run_export(ui, design, _cached_config)

            elif action == "cancel":
                palette = ui.palettes.itemById(PALETTE_ID)
                if palette:
                    palette.isVisible = False

        except Exception:
            app = adsk.core.Application.get()
            app.userInterface.messageBox(
                f"Palette handler error:\n{traceback.format_exc()}"
            )


class _PaletteCloseHandler(adsk.core.UserInterfaceGeneralEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def on_command_created(args: adsk.core.CommandCreatedEventArgs, handlers: list):
    app = adsk.core.Application.get()
    ui = app.userInterface
    design = adsk.fusion.Design.cast(app.activeProduct)

    if not design:
        ui.messageBox("No active Fusion design. Please open a design first.")
        return

    html_path = os.path.join(os.path.dirname(__file__), "palette.html")
    import time
    html_url = f"file:///{html_path.replace(os.sep, '/')}?v={int(time.time())}"

    old = ui.palettes.itemById(PALETTE_ID)
    if old:
        old.deleteMe()

    palette = ui.palettes.add(
        PALETTE_ID,
        PALETTE_TITLE,
        html_url,
        True,
        True,
        True,
        PALETTE_WIDTH,
        PALETTE_HEIGHT,
        True,
    )
    palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight

    html_handler = _PaletteHTMLHandler()
    palette.incomingFromHTML.add(html_handler)
    handlers.append(html_handler)

    close_handler = _PaletteCloseHandler()
    palette.closed.add(close_handler)
    handlers.append(close_handler)

    args.command.isOKButtonVisible = False
    cancel_handler = _CommandDestroyHandler()
    args.command.destroy.add(cancel_handler)
    handlers.append(cancel_handler)

    args.command.isAutoExecute = True


class _CommandDestroyHandler(adsk.core.CommandEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args: adsk.core.CommandEventArgs):
        pass


# ---------------------------------------------------------------------------
# Export engine
# ---------------------------------------------------------------------------

def _run_export(ui: adsk.core.UserInterface, design: adsk.fusion.Design,
                config: dict):
    """Execute the sweep + export.

    Supports two sweep modes:
      "range"    – existing behaviour: cartesian product of evenly-spaced ranges.
      "explicit" – new behaviour: zip of explicit value lists (matched columns).
    """
    try:
        output_folder: str = config.get("outputFolder", "")
        export_format: str = config.get("format", "STEP").upper()
        selected_params: list = config.get("params", [])
        selected_body_paths: list = config.get("bodies", [])
        naming_template: str = config.get("namingTemplate", "")
        sweep_mode: str = config.get("sweepMode", "range")  # "range" | "explicit"

        if not output_folder:
            ui.messageBox("No output folder selected. Aborting.")
            return
        if not os.path.isdir(output_folder):
            ui.messageBox(
                f"The output folder does not exist:\n{output_folder}\n\n"
                "Please select a valid folder and try again."
            )
            return
        if not selected_params:
            ui.messageBox("No parameters selected. Aborting.")
            return

        # ------------------------------------------------------------------
        # Build combo list depending on mode
        # axes entries: (name, [values_as_strings], unit, is_text)
        # ------------------------------------------------------------------
        if sweep_mode == "explicit":
            axes_explicit: List[Tuple[str, List[str], str, bool]] = []
            for sp in selected_params:
                name     = sp["name"]
                unit     = sp.get("unit", "")
                is_text  = sp.get("isText", False) or (unit == "" and sp.get("unit") is None)
                raw_vals = sp.get("values", []) or []

                # Determine if this is a text param: unit is empty and values
                # contain non-numeric entries
                str_vals = [str(v).strip() for v in raw_vals if v is not None and str(v).strip() != ""]
                if not is_text:
                    # Auto-detect: if any value can't be parsed as float, treat as text
                    for v in str_vals:
                        try:
                            float(v)
                        except ValueError:
                            is_text = True
                            break

                axes_explicit.append((name, str_vals, unit, is_text))

            # Validate equal lengths
            lengths = [len(vals) for _, vals, _, _ in axes_explicit]
            if len(set(lengths)) > 1:
                ui.messageBox(
                    "Explicit mode error: all parameters must have the same "
                    f"number of values.\nFound lengths: "
                    + ", ".join(f"{n}={l}" for (n, _, _, _), l in zip(axes_explicit, lengths))
                )
                return
            if not lengths or lengths[0] == 0:
                ui.messageBox("No values to export.")
                return

            # zip produces matched tuples of string values
            all_combos = list(zip(*[vals for _, vals, _, _ in axes_explicit]))
            axes = axes_explicit

        else:
            # Original range / cartesian-product mode — all numeric
            axes: List[Tuple[str, List[str], str, bool]] = []
            for sp in selected_params:
                name = sp["name"]
                unit = sp.get("unit", "")
                try:
                    low   = float(sp["low"])
                    high  = float(sp["high"])
                    steps = int(sp["steps"])
                except (TypeError, ValueError):
                    ui.messageBox(
                        f"Parameter '{name}' has non-numeric low/high values.\n\n"
                        "Only numeric parameters can be used in Range mode. "
                        "Switch to Explicit Values mode for text parameters."
                    )
                    return
                vals = [str(v) for v in _float_range(low, high, steps)]
                axes.append((name, vals, unit, False))

        total = len(all_combos)
        if total == 0:
            ui.messageBox("No combinations to export.")
            return

        confirm = ui.messageBox(
            f"This will export {total} file(s) to:\n{output_folder}\n\nContinue?",
            "Confirm Export",
            adsk.core.MessageBoxButtonTypes.YesNoButtonType,
        )
        if confirm != adsk.core.DialogResults.DialogYes:
            return

        # Resolve body references
        all_bodies = _collect_all_bodies(design)
        export_bodies = []
        if selected_body_paths:
            path_set = set(selected_body_paths)
            for body, comp, path in all_bodies:
                if path in path_set:
                    export_bodies.append((body, comp, path))
        else:
            export_bodies = all_bodies

        if not export_bodies:
            ui.messageBox("No matching bodies found. Aborting.")
            return

        all_params: Dict[str, adsk.fusion.Parameter] = {}
        for p in design.allParameters:
            all_params[p.name] = p

        original_expressions: Dict[str, str] = {}
        for name, _, _, _ in axes:
            if name in all_params:
                original_expressions[name] = all_params[name].expression

        progress = ui.createProgressDialog()
        progress.show("Exporting…", f"0 / {total}", 0, total, 1)

        export_mgr = design.exportManager
        errors = []

        for idx, combo in enumerate(all_combos):
            if progress.wasCancelled:
                break

            # Update parameters
            for (param_name, _, unit, is_text), value in zip(axes, combo):
                p = all_params.get(param_name)
                if p:
                    if is_text:
                        # Text parameters: Fusion takes the raw string value directly
                        p.expression = str(value)
                    else:
                        # Numeric parameters: value + unit
                        expr = f"{_format_value(float(value))} {unit}" if unit else _format_value(float(value))
                        p.expression = expr

            adsk.doEvents()
            design.rootComponent.isSketchFolderLightBulbOn = True
            adsk.doEvents()

            # Build file name
            parts = []
            for (param_name, _, _, is_text), value in zip(axes, combo):
                if is_text:
                    parts.append(f"{param_name}_{_sanitize(str(value))}")
                else:
                    parts.append(f"{param_name}_{_format_value(float(value))}")
            base_name = _sanitize("__".join(parts))

            if naming_template:
                tpl_name = naming_template
                for (param_name, _, _, is_text), value in zip(axes, combo):
                    display = _sanitize(str(value)) if is_text else _format_value(float(value))
                    tpl_name = tpl_name.replace(f"{{{param_name}}}", display)
                base_name = _sanitize(tpl_name)

            try:
                if export_format == "STL":
                    _export_stl(export_mgr, export_bodies, output_folder,
                                base_name, design)
                else:
                    _export_step(export_mgr, output_folder, base_name, design)
            except Exception as ex:
                errors.append(f"Combo {idx+1}: {ex}")

            progress.progressValue = idx + 1
            progress.message = f"{idx + 1} / {total}"
            adsk.doEvents()

        progress.hide()

        # Restore original parameter values
        for name, expr in original_expressions.items():
            p = all_params.get(name)
            if p:
                p.expression = expr
        adsk.doEvents()

        if errors:
            ui.messageBox(
                f"Export complete with {len(errors)} error(s):\n\n"
                + "\n".join(errors[:20])
            )
        else:
            ui.messageBox(
                f"Successfully exported {total} file(s) to:\n{output_folder}"
            )

    except Exception:
        ui.messageBox(f"Export error:\n{traceback.format_exc()}")


def _export_step(export_mgr, output_folder, base_name, design):
    filepath = os.path.join(output_folder, f"{base_name}.step")
    options = export_mgr.createSTEPExportOptions(filepath)
    export_mgr.execute(options)


def _export_stl(export_mgr, bodies, output_folder, base_name, design):
    if len(bodies) == 1:
        body, comp, path = bodies[0]
        filepath = os.path.join(output_folder, f"{base_name}.stl")
        options = export_mgr.createSTLExportOptions(body, filepath)
        options.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
        export_mgr.execute(options)
    else:
        for body, comp, path in bodies:
            body_label = _sanitize(body.name)
            filepath = os.path.join(
                output_folder, f"{base_name}__{body_label}.stl"
            )
            options = export_mgr.createSTLExportOptions(body, filepath)
            options.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium
            export_mgr.execute(options)