import argparse
import csv
import json
import re
from pathlib import Path

from src import config


RESULT_NAME_PATTERN = re.compile(
    r"^(?P<tool>[^_]+)_(?P<env>RTSTest_[A-Z]+)(?:_smoke)?(?:_[0-9.]+)?\.json$"
)


def load_json(path):
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array: {path}")
    return data


def parse_duration(value):
    """Convert evaluate.py's timedelta strings to seconds."""
    value = str(value).strip()
    days = 0
    if " day" in value:
        day_part, value = value.split(",", 1)
        days = int(day_part.split()[0])
        value = value.strip()

    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid duration: {value!r}")
    hours, minutes, seconds = parts
    return days * 86400 + int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def index_by_current(items, source):
    indexed = {}
    for item in items:
        current = item.get("current")
        if not current:
            raise ValueError(f"Missing 'current' in {source}")
        if current in indexed:
            raise ValueError(f"Duplicate commit {current} in {source}")
        indexed[current] = item
    return indexed


def infer_project_and_env(result_path):
    match = RESULT_NAME_PATTERN.match(result_path.name)
    if not match:
        raise ValueError(
            "Cannot infer the test environment from result filename "
            f"{result_path.name!r}; expected e.g. NameBDP_RTSTest_PVL.json"
        )

    env_name = match.group("env")
    matching_projects = [
        project for project, configured_env in config.ENV_NAME.items()
        if configured_env == env_name
    ]
    if len(matching_projects) != 1:
        raise ValueError(f"Cannot map environment {env_name!r} to one project")
    return matching_projects[0], env_name


def find_latest_compatible_runall(result_path, env_name, expected_commits):
    candidates = sorted(
        result_path.parent.glob(f"Runall_{env_name}_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No Runall result matching Runall_{env_name}_*.json in "
            f"{result_path.parent}"
        )

    incompatible = []
    for candidate in candidates:
        runall = load_json(candidate)
        commits = {item.get("current") for item in runall}
        if commits == expected_commits and len(runall) == len(expected_commits):
            return candidate, runall
        incompatible.append(f"{candidate.name} ({len(runall)} commits)")

    raise ValueError(
        "No Runall result has the same commit set as the RTS result. "
        f"Checked: {', '.join(incompatible)}"
    )


def safe_divide(numerator, denominator, metric_name):
    if denominator == 0:
        raise ValueError(f"Cannot compute {metric_name}: denominator is zero")
    return numerator / denominator


def calculate_metrics(result_path):
    result_path = result_path.resolve()
    rts_results = load_json(result_path)
    rts_by_commit = index_by_current(rts_results, result_path)
    expected_commits = set(rts_by_commit)

    project, env_name = infer_project_and_env(result_path)
    runall_path, runall_results = find_latest_compatible_runall(
        result_path, env_name, expected_commits
    )
    runall_by_commit = index_by_current(runall_results, runall_path)

    ground_truth_path = (
        Path(config.GROUND_TRUTH_PATH) / config.GROUND_TRUTH_FILE_NAME[project]
    )
    ground_truth = load_json(ground_truth_path)
    ground_truth_by_commit = index_by_current(ground_truth, ground_truth_path)

    missing_ground_truth = expected_commits - set(ground_truth_by_commit)
    if missing_ground_truth:
        raise ValueError(
            "Ground truth is missing commits: "
            + ", ".join(sorted(missing_ground_truth))
        )

    total_tests = 0
    selected_tests = 0
    affected_selected_tests = 0
    affected_tests = 0
    safe_commits = 0
    original_time = 0.0
    rts_time = 0.0

    for commit, rts_item in rts_by_commit.items():
        runall_item = runall_by_commit[commit]
        ground_truth_item = ground_truth_by_commit[commit]

        all_tests = set(runall_item.get("tests_to_run", []))
        selected = set(rts_item.get("tests_to_run", []))
        affected = set(ground_truth_item.get("tests_to_run", []))

        if not selected <= all_tests:
            unknown = sorted(selected - all_tests)
            raise ValueError(
                f"RTS selected tests absent from Runall at {commit}: {unknown}"
            )

        total_tests += len(all_tests)
        selected_tests += len(selected)
        affected_selected_tests += len(selected & affected)
        affected_tests += len(affected)
        safe_commits += int(affected <= selected)

        original_time += (
            parse_duration(runall_item.get("test_time", "0:00:00"))
            + parse_duration(runall_item.get("select_time", "0:00:00"))
        )
        rts_time += (
            parse_duration(rts_item.get("init_time", "0:00:00"))
            + parse_duration(rts_item.get("select_time", "0:00:00"))
            + parse_duration(rts_item.get("test_time", "0:00:00"))
        )

    commit_count = len(rts_results)
    return {
        "project": project,
        "approach": result_path.name.split("_", 1)[0],
        "commits": commit_count,
        "total_tests": total_tests,
        "selected_tests": selected_tests,
        "affected_tests": affected_tests,
        "affected_selected_tests": affected_selected_tests,
        "safe_commits": safe_commits,
        "original_time_seconds": round(original_time, 6),
        "rts_time_seconds": round(rts_time, 6),
        "test_reduction": (
            "{:.2f}%".format(safe_divide(
                total_tests - selected_tests, total_tests, "test reduction"
            ) * 100)
        ),
        "precision": (
            "{:.2f}%".format(safe_divide(
                affected_selected_tests, selected_tests, "precision"
            ) * 100)
        ),
        "time_reduction": (
            "{:.2f}%".format(safe_divide(
                original_time - rts_time, original_time, "time reduction"
            ) * 100)
        ),
        "safe_rate": (
            f"{safe_divide(safe_commits, commit_count, 'safe rate') * 100:.2f}%"
        ),
    }


def write_csv(metrics, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(metrics))
        writer.writeheader()
        writer.writerow(metrics)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Calculate RTS effectiveness and safety metrics."
    )
    parser.add_argument("result", type=Path, help="Path to an RTS result JSON")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output CSV path (default: <result_stem>_metrics.csv)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_path = args.output or args.result.with_name(
        f"{args.result.stem}_metrics.csv"
    )
    metrics = calculate_metrics(args.result)
    write_csv(metrics, output_path)
    print(f"Metrics CSV: {output_path.resolve()}")


if __name__ == "__main__":
    main()
