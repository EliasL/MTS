"""Native simulation presets and parameter sweep definitions."""

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
    return Config.read(path)


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
    elif action == "save":
        config = simulation_config(data)
        with locked(folder / ".presets.lock"):
            if path.exists() or name in BUILTIN_PRESETS:
                raise ValueError("A preset with this name already exists. Choose a new name.")
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
        values = [line.strip() for line in text.splitlines() if line.strip()]
        if kind is bool:
            if any(value not in {"true", "false"} for value in values):
                raise ValueError("Enter true or false on separate lines.")
            return [value == "true" for value in values]
        return values
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


def campaign_definition(preset_folder, data):
    from MTS_Management.config import Config, DEFAULTS, sweep

    name = checked_name(data["name"])
    base = read_preset(preset_folder, data["preset"])
    write_vtus = data.get("writeMeshVTUs")
    if write_vtus is not None:
        if type(write_vtus) is not bool:
            raise ValueError("Choose whether the campaign inherits, enables or disables mesh VTU writing.")
        base = Config(**(base.as_dict() | {"writeMeshVTUs": write_vtus}))
    parameters = DEFAULTS | {"L": 1}
    axes = {}
    size = 1
    for axis in data["axes"]:
        key = axis["parameter"]
        if key not in parameters or key in axes:
            raise ValueError(f"Unknown or repeated sweep parameter: {key}")
        if key == "writeMeshVTUs" and write_vtus is not None:
            raise ValueError("Use either the campaign override or a parameter sweep for mesh VTU writing.")
        values = axis_values(parameters[key], axis)
        size *= len(values)
        if not 1 <= size <= 1000:
            raise ValueError("A sweep must define 1–1000 runs.")
        if len(set(values)) != len(values):
            raise ValueError(f"Sweep for {key} produced duplicate values.")
        axes[key] = values
    if not axes:
        raise ValueError("Choose at least one sweep parameter.")
    configs = sweep(base, **axes)
    if any(config["writeMeshVTUs"] and config["nrVTUFrames"] <= 0 for config in configs):
        raise ValueError("Set nrVTUFrames to a positive value for runs that write mesh VTUs.")
    return {"name": name, "runs": [{"label": f"{name}-{index}", "settings": config.as_dict()}
                                    for index, config in enumerate(configs, 1)]}
