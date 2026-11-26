"""Adapt simulation form and campaign selections to manager submissions."""

from dataclasses import replace

from .monitor import site_label


def start_run(site_file, config, data, log):
    from MTS_Management.cluster import load_sites
    from MTS_Management.submission import submission_state_dir, submit_run

    name = data.get("site", "local")
    sites = load_sites(site_file)
    site = sites[name]
    if not site.ssh and site.mode != "slurm":
        site = replace(site, mode="detached")
    return submit_run(site, config, state_dir=submission_state_dir(site_file, sites),
                      campaign=data.get("campaign") or "GUI", label=data.get("label") or None,
                      initial_mesh=data.get("initial_mesh"), log=log)


def check_campaign(site_file, path, name):
    from MTS_Management.campaign import campaign_entries
    from MTS_Management.cluster import call, load_sites
    from MTS_Management.errors import ExpectedError
    from MTS_Management.runner import validate_execution_build

    site = load_sites(site_file).get(name)
    if site is None:
        raise ExpectedError("Choose an enabled execution site.")
    _, entries = campaign_entries(path)
    try:
        result = call(site, "campaign_preflight", timeout=15)
    except RuntimeError as error:
        if "Unknown remote operation: campaign_preflight" not in str(error):
            raise
        raise ExpectedError(f"Open SSH connections and click Deploy for {site_label(site.ssh or site.name)} to check campaign readiness.") from error
    try:
        if "error" in result:
            raise ExpectedError(result["error"])
        for config, _ in entries:
            validate_execution_build(config, result["build"])
    except ExpectedError as error:
        if site.ssh and str(error).startswith(("Build MTS2D", "Rebuild MTS2D")):
            if result.get("retained"):
                hint = f"{site_label(site.name)} uses a retained executable. Choose a profile with a compatible build."
            else:
                hint = f"Open SSH connections and click Build for {site_label(site.ssh)}."
            raise ExpectedError(f"{error} {hint}") from error
        raise


def start_campaign(site_file, path, name, log, *, report_progress=None):
    from MTS_Management.cluster import load_sites
    from MTS_Management.submission import submission_state_dir, submit_campaign

    sites = load_sites(site_file)
    site = sites[name]
    if not site.ssh and site.mode != "slurm":
        site = replace(site, mode="foreground")
    result = submit_campaign(site, path, state_dir=submission_state_dir(site_file, sites), workers=1,
                             log=log, report_progress=report_progress)
    skipped = len(result["skipped"])
    conflicts = {run["label"]: run["conflict"] for run in result["conflicts"]}
    if skipped:
        log(f"Skipped {skipped} completed {'run' if skipped == 1 else 'runs'}.")
    for label, detail in conflicts.items():
        log(f"{label} — {detail}")
    return result | {"errors": result.get("errors", {}) | conflicts}
