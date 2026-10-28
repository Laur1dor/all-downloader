"""Bound Docker cache and retire recognized historical images; default is dry-run."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess

LOCK = "/run/lock/adl-docker-maintenance.lock"
CACHE_ARGS = ["docker", "builder", "prune", "--all", "--force",
              "--max-used-space", "1gb", "--reserved-space", "512mb",
              "--min-free-space", "5gb"]


def history_group(tag):
    repository, _, version = tag.rpartition(":")
    if repository.startswith("tiktok") and "before" in repository:
        return "tiktok"
    if repository.startswith("anime") and "-before-" in repository:
        return "anime"
    if repository == "iglogin" and version.startswith("before-"):
        return "iglogin"
    return None


def created_at(image):
    # Docker uses RFC3339 nanoseconds; microseconds suffice for retention.
    value = image["Created"].replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def snapshot_at(image, now):
    # A newly tagged rollback can point to an old image. Retention must use
    # the snapshot date when its tag records one, not just image build time.
    dates = [created_at(image)]
    for tag in image.get("RepoTags") or []:
        for value in re.findall(r"(?<!\d)(20\d{6})(?!\d)", tag):
            try:
                dates.append(datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc))
            except ValueError:
                pass
        for value in re.findall(r"(?<!\d)(\d{4})(?!\d)", tag):
            try:
                stamp = datetime.strptime(str(now.year) + value, "%Y%m%d").replace(tzinfo=timezone.utc)
                if stamp > now + timedelta(days=1):
                    stamp = stamp.replace(year=stamp.year - 1)
                dates.append(stamp)
            except ValueError:
                pass
    return max(dates)


def plan_images(images, containers, now):
    protected = {c["Image"] for c in containers}
    groups = {}
    for image in images:
        tags = image.get("RepoTags") or []
        for group in {history_group(tag) for tag in tags} - {None}:
            groups.setdefault(group, []).append(image)
        # Any unknown/normal tag is a pin. Historical repositories sometimes
        # use ':latest'; those are history, not the service's actual latest.
        if not tags or any(history_group(tag) is None for tag in tags):
            protected.add(image["Id"])
    for rows in groups.values():
        rows.sort(key=lambda x: (snapshot_at(x, now), created_at(x), x["Id"]), reverse=True)
        protected.update(row["Id"] for row in rows[:2])
    cutoff = now - timedelta(days=7)
    return [image for image in images
            if image["Id"] not in protected and snapshot_at(image, now) < cutoff]


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=kwargs.pop("timeout", 60), **kwargs)


def inventory():
    container_ids = command(["docker", "ps", "-aq"]).split()
    image_ids = sorted(set(command(["docker", "image", "ls", "-aq", "--no-trunc"]).split()))
    containers = json.loads(command(["docker", "inspect", *container_ids])) if container_ids else []
    images = json.loads(command(["docker", "image", "inspect", *image_ids])) if image_ids else []
    return images, containers


def build_in_progress():
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            argv = (path / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or not argv[0]:
            continue
        executable = Path(argv[0].decode(errors="replace")).name
        if executable in ("docker", "docker-compose", "docker-buildx") and b"build" in argv[1:]:
            return True
    return False


def main():
    import fcntl
    import shutil

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute the reviewed retention policy")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with open(LOCK, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Docker maintenance skipped: deployment or cleanup holds the lock")
            return
        state = command(["systemctl", "show", "selfdeploy.service", "-p", "ActiveState", "--value"]).strip()
        if state not in ("inactive", "failed") or build_in_progress():
            print("Docker maintenance skipped: deployment/build in progress")
            return
        images, containers = inventory()
        candidates = plan_images(images, containers, datetime.now(timezone.utc))
        before = shutil.disk_usage("/")
        report = {"apply": args.apply, "free_before": before.free,
                  "candidates": [{"id": x["Id"], "tags": x["RepoTags"]} for x in candidates],
                  "removed_tags": [], "errors": [], "cache_policy": CACHE_ARGS[3:],
                  "containers_before": {c["Id"]: {"name": c["Name"], "image": c["Image"],
                       "running": c["State"]["Running"]} for c in containers}}
        if args.apply:
            for image in candidates:
                # Recheck all pins, tags and container references immediately
                # before removal. Never use force; Docker also guards races.
                fresh_images, fresh_containers = inventory()
                allowed = {x["Id"]: x for x in plan_images(
                    fresh_images, fresh_containers, datetime.now(timezone.utc))}
                current = allowed.get(image["Id"])
                if not current or set(current.get("RepoTags") or []) != set(image["RepoTags"]):
                    continue
                try:
                    command(["docker", "image", "rm", *current["RepoTags"]],
                            timeout=180, stderr=subprocess.STDOUT)
                    report["removed_tags"].extend(current["RepoTags"])
                except subprocess.CalledProcessError as error:
                    report["errors"].append(error.output[-1500:])
                except subprocess.TimeoutExpired as error:
                    report["errors"].append(str(error))
            try:
                report["dangling_image_summary"] = command(
                    ["docker", "image", "prune", "--force"], stderr=subprocess.STDOUT).splitlines()[-1:]
            except subprocess.CalledProcessError as error:
                report["errors"].append(error.output[-1500:])
            # BuildKit only removes unused cache. '--all' includes old cache
            # types omitted by the old dangling-only cleanup; images remain.
            try:
                output = command(CACHE_ARGS, timeout=600, stderr=subprocess.STDOUT)
                report["cache_summary"] = output.splitlines()[-1:] if output else []
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                report["errors"].append(str(error))
            _, after_containers = inventory()
            after_by_id = {c["Id"]: c for c in after_containers}
            report["containers_preserved"] = all(
                c["Id"] in after_by_id and after_by_id[c["Id"]]["Image"] == c["Image"]
                and after_by_id[c["Id"]]["State"]["Running"] == c["State"]["Running"]
                for c in containers)
            if not report["containers_preserved"]:
                report["errors"].append("Container state changed during cleanup; inspect concurrent activity")
        report["free_after"] = shutil.disk_usage("/").free
        report["free_space_target_met"] = report["free_after"] >= 5 * 1024**3
        text = json.dumps(report, indent=2)
        if args.report:
            temporary = args.report.with_suffix(args.report.suffix + ".tmp")
            temporary.write_text(text)
            temporary.replace(args.report)
        print(text)
        if report["errors"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
