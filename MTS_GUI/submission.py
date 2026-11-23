"""Adapt simulation form and campaign selections to manager submissions."""

from dataclasses import replace


def start_run(site_file, config, data, log):
    from MTS_Management.cluster import load_sites
    from MTS_Management.submission import submission_state_dir, submit_run

    name = data.get("site", "local")
    sites = load_sites(site_file)
    site = replace(sites[name], mode="detached") if name == "local" else sites[name]
    return submit_run(site, config, state_dir=submission_state_dir(site_file, sites),
                      campaign="GUI", label=data.get("label") or None,
                      initial_mesh=data.get("initial_mesh"), log=log)


def start_campaign(site_file, path, name, log):
    from MTS_Management.cluster import load_sites
    from MTS_Management.submission import submission_state_dir, submit_campaign

    sites = load_sites(site_file)
    site = replace(sites[name], mode="foreground") if name == "local" else sites[name]
    result = submit_campaign(site, path, state_dir=submission_state_dir(site_file, sites), workers=1, log=log)
    skipped = len(result["skipped"])
    errors = {run["label"]: run["conflict"] for run in result["conflicts"]}
    if skipped:
        log(f"Skipped {skipped} completed {'run' if skipped == 1 else 'runs'}.")
    for label, detail in errors.items():
        log(f"{label} — {detail}")
    return result | {"errors": errors}
