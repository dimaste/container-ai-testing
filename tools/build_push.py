#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional


def run(cmd: str, cwd: Optional[Path] = None) -> None:
    print(f"\\n$ {cmd}")
    subprocess.run(cmd, shell=True, cwd=str(cwd) if cwd else None, check=True)


def safe_tag(value: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in value)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dockerfile_for_case(base_image: str, case: dict[str, Any], canary: str) -> str:
    cid = case["id"]
    carrier = case["carrier"]
    payload = case["payload"]

    # Shared metadata for traceability
    lines = [
        f"FROM {base_image}",
        f'LABEL org.llmsec.case_id="{cid}"',
        f'LABEL org.llmsec.canary="{canary}"',
        f'LABEL org.llmsec.carrier="{carrier}"',
    ]

    if carrier == "label":
        lines.append(f'LABEL org.llmsec.payload="{payload.replace(chr(34), r"\\\"")}"')
    elif carrier == "env":
        lines.append(f'ENV LLMSEC_PAYLOAD="{payload.replace(chr(34), r"\\\"")}"')
    elif carrier == "cmd":
        escaped = payload.replace("\\", "\\\\").replace("\"", "\\\"")
        lines.append(f'CMD ["sh", "-lc", "echo \"{escaped}\""]')
    elif carrier == "file":
        path = case.get("path")
        if not path:
            raise ValueError(f"case {cid}: carrier=file requires 'path'")
        parent = shlex.quote(str(Path(path).parent))
        target = shlex.quote(path)
        lines.extend(
            [
                f"RUN mkdir -p {parent}",
                f"RUN cat > {target} << 'EOF'",
                payload,
                "EOF",
            ]
        )
    else:
        raise ValueError(f"case {cid}: unknown carrier '{carrier}'")

    return "\\n".join(lines) + "\\n"


def validate_case(case: dict[str, Any]) -> None:
    required = ["id", "carrier", "payload"]
    missing = [field for field in required if field not in case or not str(case[field]).strip()]
    if missing:
        raise ValueError(f"case is missing required fields: {', '.join(missing)}")

    if case["carrier"] == "file" and not str(case.get("path", "")).strip():
        raise ValueError(f"case {case['id']}: carrier=file requires non-empty 'path'")


def get_effective_settings(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config)
    config = load_json(config_path)

    defaults = {
        "repo": "llmsec",
        "outdir": "out",
        "push": False,
        "pull_base": False,
        "tag_prefix": "",
        "timestamp_format": "%Y%m%d%H%M%S",
    }
    settings = {**defaults, **config}

    for key in ["base_image", "registry", "suite"]:
        if key not in settings or not str(settings[key]).strip():
            raise ValueError(f"missing required config field: {key}")

    # CLI overrides (optional)
    for key in ["base_image", "registry", "repo", "suite", "outdir", "tag_prefix", "timestamp_format"]:
        value = getattr(args, key)
        if value is not None:
            settings[key] = value

    if args.push:
        settings["push"] = True
    if args.no_push:
        settings["push"] = False
    if args.pull_base:
        settings["pull_base"] = True
    if args.no_pull_base:
        settings["pull_base"] = False

    return settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build mutated image variants from a suite and optionally push them to a registry."
    )
    parser.add_argument("--config", default="config/build_push.config.json", help="Path to JSON config file")

    # Optional overrides for reuse across environments
    parser.add_argument("--base-image", dest="base_image")
    parser.add_argument("--registry")
    parser.add_argument("--repo")
    parser.add_argument("--suite")
    parser.add_argument("--outdir")
    parser.add_argument("--tag-prefix", dest="tag_prefix")
    parser.add_argument("--timestamp-format", dest="timestamp_format")

    parser.add_argument("--push", action="store_true", help="Override config: push images")
    parser.add_argument("--no-push", action="store_true", help="Override config: do not push images")
    parser.add_argument("--pull-base", action="store_true", help="Override config: pull base image")
    parser.add_argument("--no-pull-base", action="store_true", help="Override config: do not pull base image")

    args = parser.parse_args()
    settings = get_effective_settings(args)

    suite_path = Path(settings["suite"])
    outdir = Path(settings["outdir"])
    outdir.mkdir(parents=True, exist_ok=True)

    cases = load_json(suite_path)
    if not isinstance(cases, list):
        raise ValueError("suite file must contain a JSON array of case objects")

    for case in cases:
        validate_case(case)

    ts = dt.datetime.utcnow().strftime(settings["timestamp_format"])
    suite_name = safe_tag(suite_path.stem)

    if settings["pull_base"]:
        run(f"docker pull {shlex.quote(settings['base_image'])}")

    runlist = {
        "generated_at_utc": ts,
        "base_image": settings["base_image"],
        "registry": settings["registry"],
        "repo": settings["repo"],
        "suite": str(suite_path),
        "images": [],
    }

    for case in cases:
        cid = safe_tag(case["id"])
        canary = f"PWNED_CANARY_{cid}"

        tag_core = f"{suite_name}-{cid}-{ts}"
        if settings["tag_prefix"]:
            tag_core = f"{safe_tag(settings['tag_prefix'])}-{tag_core}"

        tag = f"{settings['registry']}/{settings['repo']}:{tag_core}"

        workdir = outdir / f"work_{suite_name}_{cid}_{ts}"
        workdir.mkdir(parents=True, exist_ok=True)

        dockerfile = dockerfile_for_case(settings["base_image"], case, canary)
        (workdir / "Dockerfile").write_text(dockerfile, encoding="utf-8")

        run(f"docker build -t {shlex.quote(tag)} -f Dockerfile .", cwd=workdir)

        if settings["push"]:
            run(f"docker push {shlex.quote(tag)}")

        runlist["images"].append(
            {
                "case_id": case["id"],
                "carrier": case["carrier"],
                "tag": tag,
                "canary": canary,
                "path": case.get("path"),
                "payload_preview": case["payload"][:140] + ("..." if len(case["payload"]) > 140 else ""),
            }
        )

    out_file = outdir / f"runlist_{suite_name}_{ts}.json"
    out_file.write_text(json.dumps(runlist, indent=2), encoding="utf-8")
    print(f"\\nWrote runlist: {out_file}")


if __name__ == "__main__":
    main()
