"""Native simulation presets and parameter sweep definitions."""

import csv
from io import StringIO
import math
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


BUILTIN_PRESETS = {
    "Example-simple-shear": dict(
        rows=100, cols=100, nrThreads=3, boundary="periodic", startLoad=0.15,
        totalLoad=0.85, loadIncrement=1e-5, LBFGSEpsx=1e-6,
        energyDropThreshold=1e-4, writeMeshVTUs=True,
    ),
    "Sequential-slip": dict(
        rows=10, cols=10, deformation="sequentialSlip", boundary="anchored",
        directions="1,2,2", loadIncrement=1e-3, reconnectionMethod="edgeFlip",
        meshDiagonal="minor", epsR=1e-6, writeMeshVTUs=True,
    ),
}


def preset_names(folder):
    return sorted(BUILTIN_PRESETS.keys() | {path.stem for path in folder.glob("*.conf")})


def read_preset(folder, name):
    from MTS_Management.config import Config

    path = folder / (checked_name(name) + ".conf")
    if not path.exists() and name in BUILTIN_PRESETS:
        return Config(**BUILTIN_PRESETS[name])
    # Temporary: read pre-rate presets through the shared recorded-settings
    # adapter until these files have been saved in the current schema.
    return Config(**Config.read_saved(path).effective_dict())


def checked_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", name):
        raise ValueError("Use 1–80 ASCII letters, digits, dots, underscores or hyphens.")
    return name


def simulation_config(data):
    from MTS_Management.config import Config

    name, overrides = Config.parse_settings(data.get("overrides", ""))
    if name is not None:
        raise ValueError("Set the run label in the form.")
    if overlap := data["settings"].keys() & overrides.keys():
        raise ValueError(f"Use the form for these settings: {', '.join(sorted(overlap))}")
    return Config(**(data["settings"] | overrides))


def preset(folder, data):
    from MTS_Management.config import Config
    from MTS_Management.files import locked

    action = data["action"]
    if action == "import":
        name, settings = Config.parse_settings(data["text"])
        return {"settings": Config(**settings).as_dict(), "label": name.split("[", 1)[0] if name else ""}
    name = checked_name(data["name"])
    path = folder / (name + ".conf")
    if action == "load":
        config = read_preset(folder, name)
    elif action in {"save", "update"}:
        config = simulation_config(data)
        with locked(folder / ".presets.lock"):
            exists = path.exists() or name in BUILTIN_PRESETS
            if action == "save" and exists:
                raise ValueError("A preset with this name already exists. Use Update preset.")
            if action == "update" and not exists:
                raise ValueError("This preset is no longer available. Use Save preset.")
            with NamedTemporaryFile(mode="w", dir=folder, delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(config.text(config.folder_name(name)))
                    stream.close()
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
    else:
        raise ValueError("Unknown preset action")
    return {"settings": config.as_dict(), "text": config.text(config.folder_name(name))}


def axis_values(default, axis):
    mode = axis["mode"]
    kind = type(default)
    if mode == "custom":
        text = axis["values"].strip()
        if kind in (int, float):
            return [kind(value) for value in re.split(r"[,\s]+", text) if value]
        if kind is bool:
            values = [value for value in re.split(r"[,\s]+", text) if value]
            if any(value not in {"true", "false"} for value in values):
                raise ValueError("Enter true or false, separated by commas.")
            return [value == "true" for value in values]
        if axis.get("parameter") == "directions" and '"' not in text:
            # Existing sweep drafts use one unquoted sequence per line.
            return [line.strip() for line in text.splitlines() if line.strip()]
        try:
            rows = csv.reader(text.splitlines(), skipinitialspace=True, strict=True)
            return [value.strip() for row in rows for value in row]
        except csv.Error as error:
            raise ValueError("Separate values with commas and quote values that contain commas.") from error
    if kind not in (int, float) or mode not in {"linear", "log"}:
        raise ValueError("Linear and logarithmic sweeps require a numeric parameter.")
    start, stop, count = float(axis["start"]), float(axis["stop"]), int(str(axis["count"]))
    if not (math.isfinite(start) and math.isfinite(stop) and 2 <= count <= 1000):
        raise ValueError("Enter finite endpoints and 2–1000 samples.")
    if mode == "log" and min(start, stop) <= 0:
        raise ValueError("Logarithmic sweep endpoints must be positive.")
    values = [start]
    for i in range(1, count - 1):
        weight = i / (count - 1)
        values.append((1 - weight) * start + weight * stop if mode == "linear" else
                      math.exp((1 - weight) * math.log(start) + weight * math.log(stop)))
    values.append(stop)
    return [round(value) for value in values] if kind is int else values


def restore_axis(parameter, samples, default):
    if type(default) in (int, float) and 2 <= len(samples) <= 1000:
        ordered = sorted(samples)
        endpoints = {"parameter": parameter, "start": str(ordered[0]), "stop": str(ordered[-1]),
                     "count": str(len(ordered))}
        for mode in ("linear", "log"):
            axis = endpoints | {"mode": mode}
            try:
                if axis_values(default, axis) == ordered:
                    return axis
            except (OverflowError, ValueError):
                continue
    text = StringIO()
    quoting = csv.QUOTE_ALL if parameter == "directions" else csv.QUOTE_MINIMAL
    writer = csv.writer(text, lineterminator="", quoting=quoting)
    writer.writerow(str(value).lower() if type(value) is bool else value for value in samples)
    return {"parameter": parameter, "mode": "custom", "values": text.getvalue()}


def campaign_base(runs):
    return next(((config, label) for config, label in runs if not (
        config["logDuringMinimization"] or config["fullMinimizationLogging"])), runs[0])


def campaign_plan(path):
    """Read the runs and editor recipe for regenerating a campaign."""
    from MTS_Management.campaign import saved_campaign_entries
    from MTS_Management.files import read_json

    name, runs = saved_campaign_entries(path)
    resource_path = path.with_name("resources.json")
    resources = read_json(resource_path) if resource_path.is_file() else {}
    metadata_path = path.with_name("metadata.json")
    metadata = read_json(metadata_path) if metadata_path.is_file() else {}
    recipe = metadata.get("sweep")
    if recipe is not None and recipe.get("version") != 1:
        raise ValueError("Unsupported saved campaign sweep version.")
    diagnostic = resources.get("diagnostic_label")
    extras = set(recipe.get("extra_labels", []) if recipe is not None else [diagnostic] if diagnostic else [])
    if extras - {label for _, label in runs}:
        raise ValueError("The campaign identifies an extra run that is absent from its definition.")
    planned = [(config, label) for config, label in runs if label not in extras]
    if not planned:
        raise ValueError("The campaign has no planned runs.")
    return name, planned, resources.get("site"), recipe


def campaign_editor(path):
    """Load the saved recipe or recover editable sweep values for older definitions."""
    from MTS_Management.config import Config, DEFAULTS, sweep

    name, runs, site, recipe = campaign_plan(path)
    base, label = campaign_base(runs)
    if recipe is not None:
        base = Config(**recipe["base"])
        axes = campaign_axes(recipe["axes"], limit=None if recipe["preserve_runs"] else max(1000, len(runs)))
        if recipe["preserve_runs"]:
            matches_plan = len(filter_campaign_members(runs, axes)) == len(runs)
        else:
            expected = {Config(**config.effective_dict()).config_hash for config, _ in runs}
            matches_plan = {config.config_hash for config in sweep(base, **axes)} == expected
        if not matches_plan:
            raise ValueError("The saved sweep recipe differs from the planned run list.")
        return {"name": name, "settings": base.effective_dict(), "label": label,
                "site": site, "runs": len(runs), "preserve_runs": recipe["preserve_runs"], "axes": recipe["axes"],
                "writes_vtus": any(config["writeMeshVTUs"] for config, _ in runs)}
    settings = [DEFAULTS | config.effective_dict() for config, _ in runs]
    values = {key: list(dict.fromkeys(row[key] for row in settings)) for key in settings[0]}
    axes = {key: samples for key, samples in values.items() if len(samples) > 1}
    if "rows" in axes and all(row["rows"] == row["cols"] for row in settings):
        axes["L"] = axes.pop("rows")
        del axes["cols"]
    if "directions" in axes and all(row["deformation"] == "sequentialSlip" for row in settings):
        axes.pop("totalLoad", None)
    exact = False
    if ("initialMeshSha256" not in axes and axes
            and math.prod(map(len, axes.values())) == len(runs)):
        try:
            expected = {Config(**row).config_hash for row in settings}
            exact = {config.config_hash for config in sweep(base, **axes)} == expected
        except ValueError:
            # The independent axes can produce invalid combinations for a
            # campaign whose parameters are constrained together.
            pass
    parameters = DEFAULTS | {"L": 1}
    return {"name": name, "settings": base.effective_dict(), "label": label, "site": site,
            "runs": len(runs), "preserve_runs": not exact,
            "axes": [restore_axis(key, samples, parameters[key]) for key, samples in axes.items()],
            "writes_vtus": any(values["writeMeshVTUs"])}


def campaign_axes(rows, *, limit=None, write_vtus=None):
    from MTS_Management.config import DEFAULTS

    parameters = DEFAULTS | {"L": 1}
    axes = {}
    size = 1
    for axis in rows:
        key = axis["parameter"]
        if key not in parameters or key in axes:
            raise ValueError(f"Unknown or repeated sweep parameter: {key}")
        if key == "writeMeshVTUs" and write_vtus is not None:
            raise ValueError("Use either the campaign override or a parameter sweep for mesh VTU writing.")
        values = axis_values(parameters[key], axis)
        if not values:
            raise ValueError(f"Enter at least one value for {key}.")
        if len(set(values)) != len(values):
            raise ValueError(f"Sweep for {key} produced duplicate values.")
        size *= len(values)
        if limit is not None and size > limit:
            raise ValueError(f"A sweep must define 1–{limit} runs.")
        axes[key] = values
    return axes


def sweep_settings(config):
    from MTS_Management.config import DEFAULTS

    size = config["rows"] if config["rows"] == config["cols"] else None
    return DEFAULTS | config.effective_dict() | {"L": size}


def filter_campaign_members(members, axes):
    if not axes:
        return members
    requested = {key: set(values) for key, values in axes.items()}
    selected = []
    for config, label in members:
        settings = sweep_settings(config)
        if all(settings[key] in values for key, values in requested.items()):
            selected.append((config, label))
    return selected


def campaign_size(data, *, source=None):
    members = campaign_plan(source)[1] if source is not None else []
    if source is not None and data.get("preserve_runs"):
        return len(filter_campaign_members(members, campaign_axes(data["axes"])))
    axes = campaign_axes(data["axes"], limit=max(1000, len(members)), write_vtus=data.get("writeMeshVTUs"))
    return math.prod(map(len, axes.values())) if axes else 0


def campaign_definition(preset_folder, data, *, source=None):
    from MTS_Management.config import Config, DEFAULTS, sweep

    name = checked_name(data["name"])
    members = campaign_plan(source)[1] if source is not None else []
    base = simulation_config(data["base"]) if source is not None else read_preset(preset_folder, data["preset"])
    write_vtus = data.get("writeMeshVTUs")
    if write_vtus is not None:
        if type(write_vtus) is not bool:
            raise ValueError("Choose whether the campaign inherits, enables or disables mesh VTU writing.")
        base = Config(**(base.effective_dict() | {"writeMeshVTUs": write_vtus}))
    preserve_runs = source is not None and bool(data.get("preserve_runs"))
    recipe = {"version": 1, "base": base.effective_dict(), "axes": list(data["axes"]), "preserve_runs": preserve_runs}
    if preserve_runs:
        original = DEFAULTS | campaign_editor(source)["settings"]
        changes = {key: value for key, value in base.effective_dict().items() if value != original[key]}
        if write_vtus is not None:
            changes["writeMeshVTUs"] = write_vtus
        axes = campaign_axes(data["axes"])
        members = filter_campaign_members(members, axes)
        if not members:
            raise ValueError("No saved campaign runs match these sweep values.")
        overrides = {key: [value] for key, value in changes.items()}
        configs = [sweep(config, **overrides)[0] for config, _ in members]
        if len({config.config_hash for config in configs}) != len(configs):
            raise ValueError("The edited settings give multiple campaign runs the same configuration.")
        resolved = [sweep_settings(config) for config in configs]
        for index, axis in enumerate(recipe["axes"]):
            key = axis["parameter"]
            samples = list(dict.fromkeys(settings[key] for settings in resolved))
            if set(samples) - set(axes[key]):
                if key == "L" and None in samples:
                    raise ValueError("Use row and column filters when editing runs to rectangular sizes.")
                recipe["axes"][index] = restore_axis(key, samples, 1 if key == "L" else DEFAULTS[key])
        return {"name": name, "runs": [{"label": label, "settings": config.as_dict()}
                                       for config, (_, label) in zip(configs, members, strict=True)]}, recipe
    axes = campaign_axes(data["axes"], limit=max(1000, len(members)), write_vtus=write_vtus)
    if not axes:
        raise ValueError("Choose at least one sweep parameter.")
    configs = sweep(base, **axes)
    return {"name": name, "runs": [{"label": f"{name}-{index}", "settings": config.as_dict()}
                                    for index, config in enumerate(configs, 1)]}, recipe
