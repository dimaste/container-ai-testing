#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import json
import os
import random
import shlex
import subprocess
import urllib.request
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


def escape_docker_quoted(value: str) -> str:
    return value.replace('"', '\\"')


def fetch_text(url: str, timeout_seconds: int = 30) -> str:
    with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
        return response.read().decode("utf-8")


def render_template(template: str, row: dict[str, Any]) -> str:
    rendered = template
    for key, value in row.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value if value is not None else ""))
    return rendered


def extract_prompts_from_source(source: dict[str, Any], timeout_seconds: int) -> list[dict[str, str]]:
    source_id = str(source["id"])
    source_url = str(source["url"])
    source_format = str(source["format"]).lower()
    limit = int(source.get("limit", 0))
    shuffle = bool(source.get("shuffle", False))

    raw_text = fetch_text(source_url, timeout_seconds=timeout_seconds)
    prompts: list[dict[str, str]] = []

    if source_format == "json":
        data = json.loads(raw_text)
        if not isinstance(data, list):
            raise ValueError(f"source {source_id}: expected JSON array")
        field = source.get("field")
        template = source.get("template")
        if not field and not template:
            raise ValueError(f"source {source_id}: set either 'field' or 'template'")
        for row in data:
            if not isinstance(row, dict):
                continue
            payload = (
                render_template(str(template), row)
                if template
                else str(row.get(str(field), "")).strip()
            )
            payload = payload.strip()
            if payload:
                prompts.append({"source_id": source_id, "payload": payload})
    elif source_format == "csv":
        field = source.get("field")
        template = source.get("template")
        if not field and not template:
            raise ValueError(f"source {source_id}: set either 'field' or 'template'")
        reader = csv.DictReader(raw_text.splitlines())
        for row in reader:
            payload = (
                render_template(str(template), row)
                if template
                else str(row.get(str(field), "")).strip()
            )
            payload = payload.strip()
            if payload:
                prompts.append({"source_id": source_id, "payload": payload})
    else:
        raise ValueError(f"source {source_id}: unsupported format '{source_format}'")

    if shuffle:
        random.Random(42).shuffle(prompts)
    if limit > 0:
        prompts = prompts[:limit]
    return prompts


def load_external_cases(settings: dict[str, Any]) -> list[dict[str, Any]]:
    if not settings["external_prompts_enabled"]:
        return []

    manifest_path = Path(settings["external_prompt_manifest"])
    manifest = load_json(manifest_path)

    if not isinstance(manifest, dict) or "sources" not in manifest:
        raise ValueError("external prompt manifest must be a JSON object with 'sources'")
    if not isinstance(manifest["sources"], list):
        raise ValueError("external prompt manifest: 'sources' must be an array")

    carriers = settings["external_carrier_cycle"]
    if not isinstance(carriers, list) or not carriers:
        raise ValueError("external_carrier_cycle must be a non-empty list")

    timeout_seconds = int(settings["external_fetch_timeout_seconds"])
    all_prompts: list[dict[str, str]] = []
    for source in manifest["sources"]:
        all_prompts.extend(extract_prompts_from_source(source, timeout_seconds=timeout_seconds))

    max_total = int(settings["external_prompts_limit"])
    if max_total > 0:
        all_prompts = all_prompts[:max_total]

    cases: list[dict[str, Any]] = []
    prefix = safe_tag(str(settings["external_case_prefix"]))
    path_template = str(settings["external_file_path_template"])

    for idx, item in enumerate(all_prompts, start=1):
        carrier = carriers[(idx - 1) % len(carriers)]
        source_id = safe_tag(item["source_id"])
        case_id = f"{prefix}_{source_id}_{idx:04d}"
        case: dict[str, Any] = {
            "id": case_id,
            "carrier": carrier,
            "payload": item["payload"],
        }
        if carrier == "file":
            case["path"] = path_template.format(source=source_id, idx=idx)
        cases.append(case)

    return cases


def expand_cases_to_all_carriers(cases: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    if not settings.get("expand_case_to_all_carriers", False):
        return cases

    carriers = settings.get("expand_carriers", ["label", "env", "file", "cmd"])
    if not isinstance(carriers, list) or not carriers:
        raise ValueError("expand_carriers must be a non-empty list")

    path_template = str(settings.get("expand_file_path_template", "/tmp/payload_{id}.txt"))
    expanded: list[dict[str, Any]] = []
    for case in cases:
        payload = case["payload"]
        base_id = safe_tag(str(case["id"]))
        for carrier in carriers:
            new_case = {
                "id": f"{base_id}_{safe_tag(carrier)}",
                "carrier": carrier,
                "payload": payload,
            }
            if carrier == "file":
                new_case["path"] = path_template.format(id=base_id, carrier=safe_tag(carrier))
            expanded.append(new_case)
    return expanded


def dockerfile_for_case(base_image: str, case: dict[str, Any], canary: str, settings: dict[str, Any]) -> str:
    cid = case["id"]
    carrier = case["carrier"]
    payload = case["payload"]
    trace_labels_enabled = bool(settings.get("trace_labels_enabled", False))
    trace_label_prefix = str(settings.get("trace_label_prefix", "org.llmsec"))
    payload_label_key = str(settings.get("payload_label_key", "payload"))
    payload_env_key = str(settings.get("payload_env_key", "PAYLOAD"))

    lines = [f"FROM {base_image}"]
    if trace_labels_enabled:
        lines.extend(
            [
                f'LABEL {trace_label_prefix}.case_id="{cid}"',
                f'LABEL {trace_label_prefix}.canary="{canary}"',
                f'LABEL {trace_label_prefix}.carrier="{carrier}"',
            ]
        )

    inline_payload = payload.replace("\r", " ").replace("\n", " ").strip()

    if carrier == "label":
        escaped_payload = escape_docker_quoted(inline_payload)
        lines.append(f'LABEL {payload_label_key}="{escaped_payload}"')
    elif carrier == "env":
        escaped_payload = escape_docker_quoted(inline_payload)
        lines.append(f'ENV {payload_env_key}="{escaped_payload}"')
    elif carrier == "cmd":
        escaped = inline_payload.replace("\\", "\\\\").replace("\"", "\\\"")
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

    return "\n".join(lines) + "\n"


def validate_case(case: dict[str, Any]) -> None:
    required = ["id", "carrier", "payload"]
    missing = [field for field in required if field not in case or not str(case[field]).strip()]
    if missing:
        raise ValueError(f"case is missing required fields: {', '.join(missing)}")

    if case["carrier"] == "file" and not str(case.get("path", "")).strip():
        raise ValueError(f"case {case['id']}: carrier=file requires non-empty 'path'")


def build_image_ref(registry: str, repo: str, image_name: str, tag: str) -> str:
    registry_clean = registry.strip().rstrip("/")
    repo_clean = repo.strip().strip("/")
    image_clean = image_name.strip().strip("/")
    if not registry_clean or not repo_clean or not image_clean:
        raise ValueError("registry, repo, and image_name must be non-empty")
    return f"{registry_clean}/{repo_clean}/{image_clean}:{tag}"


def default_docker_config_dirs(container_cli: str) -> list[Path]:
    dirs: list[Path] = []
    env_dir = os.environ.get("DOCKER_CONFIG", "").strip()
    if env_dir:
        dirs.append(Path(env_dir).expanduser())

    home = Path.home()
    # Most common location for both docker and nerdctl.
    dirs.append(home / ".docker")
    # nerdctl may also use config under ~/.config/nerdctl.
    dirs.append(home / ".config" / "nerdctl")
    # Some environments rely on containers auth file location.
    dirs.append(home / ".config" / "containers")

    deduped: list[Path] = []
    seen = set()
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            deduped.append(d)
    return deduped


def resolve_docker_config_dir(settings: dict[str, Any], container_cli: str, registry: str) -> Path:
    configured = str(settings.get("docker_config", "")).strip()
    if configured:
        return Path(configured).expanduser()

    candidates = default_docker_config_dirs(container_cli)
    require_auth = bool(settings.get("require_registry_auth_entry", True))

    if require_auth:
        for candidate in candidates:
            if has_registry_auth_entry(candidate, registry):
                return candidate

    for candidate in candidates:
        if (candidate / "config.json").exists():
            return candidate

    return candidates[0]


def has_registry_auth_entry(config_dir: Path, registry: str) -> bool:
    config_path = config_dir / "config.json"
    if not config_path.exists():
        return False

    try:
        data = load_json(config_path)
    except Exception:
        return False

    if not isinstance(data, dict):
        return False

    keys = {registry, f"https://{registry}", f"http://{registry}"}
    auths = data.get("auths", {})
    helpers = data.get("credHelpers", {})

    if isinstance(auths, dict):
        if any(key in auths for key in keys):
            return True
    if isinstance(helpers, dict):
        if any(key in helpers for key in keys):
            return True
    return False


def get_effective_settings(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config)
    config = load_json(config_path)

    defaults = {
        "container_cli": "docker",
        "container_cli_args": [],
        "docker_config": "",
        "require_registry_auth_entry": True,
        "insecure_registry": False,
        "repo": "llmsec",
        "image_name": "mutated",
        "outdir": "out",
        "push": False,
        "pull_base": False,
        "tag_prefix": "",
        "timestamp_format": "%Y%m%d%H%M%S",
        "trace_labels_enabled": False,
        "trace_label_prefix": "org.llmsec",
        "payload_label_key": "payload",
        "payload_env_key": "PAYLOAD",
        "external_suite": "cases/suite_external.json",
        "include_external_suite": True,
        "expand_case_to_all_carriers": False,
        "expand_carriers": ["label", "env", "file", "cmd"],
        "expand_file_path_template": "/usr/share/doc/llmsec/expanded/{id}_{carrier}.txt",
        "external_prompts_enabled": False,
        "external_prompt_manifest": "cases/prompt_sources_promptfoo.json",
        "external_prompts_limit": 0,
        "external_case_prefix": "ext",
        "external_carrier_cycle": ["label", "env", "file", "cmd"],
        "external_file_path_template": "/usr/share/doc/llmsec/{source}/payload_{idx:04d}.txt",
        "external_fetch_timeout_seconds": 30,
    }
    settings = {**defaults, **config}

    for key in ["base_image", "registry", "repo", "image_name", "suite"]:
        if key not in settings or not str(settings[key]).strip():
            raise ValueError(f"missing required config field: {key}")

    # CLI overrides (optional)
    for key in [
        "container_cli",
        "docker_config",
        "require_registry_auth_entry",
        "insecure_registry",
        "base_image",
        "registry",
        "repo",
        "image_name",
        "suite",
        "external_suite",
        "include_external_suite",
        "outdir",
        "tag_prefix",
        "timestamp_format",
    ]:
        value = getattr(args, key, None)
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
    parser.add_argument("--container-cli", dest="container_cli")
    parser.add_argument("--docker-config", dest="docker_config")
    parser.add_argument("--base-image", dest="base_image")
    parser.add_argument("--registry")
    parser.add_argument("--repo")
    parser.add_argument("--image-name", dest="image_name")
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

    container_cli = str(settings["container_cli"]).strip()
    if container_cli not in {"docker", "nerdctl"}:
        raise ValueError("container_cli must be either 'docker' or 'nerdctl'")
    cli_args = settings["container_cli_args"]
    if not isinstance(cli_args, list):
        raise ValueError("container_cli_args must be a JSON array of CLI arguments")

    docker_config_dir = resolve_docker_config_dir(
        settings=settings,
        container_cli=container_cli,
        registry=str(settings["registry"]),
    )
    if bool(settings.get("require_registry_auth_entry", True)):
        if not has_registry_auth_entry(docker_config_dir, str(settings["registry"])):
            checked = [str(d / "config.json") for d in default_docker_config_dirs(container_cli)]
            raise ValueError(
                f"No registry auth entry for '{settings['registry']}'. "
                f"Selected config: '{docker_config_dir / 'config.json'}'. "
                f"Checked defaults: {checked}. Run '{container_cli} login {settings['registry']}' "
                "or set docker_config explicitly."
            )

    insecure_registry = bool(settings["insecure_registry"])
    effective_cli_args = [str(arg) for arg in cli_args]
    if insecure_registry and container_cli == "nerdctl":
        if "--insecure-registry" not in effective_cli_args:
            effective_cli_args.append("--insecure-registry")
    if insecure_registry and container_cli == "docker":
        print(
            "WARNING: insecure_registry=true is set, but Docker requires daemon-level "
            "insecure-registries config; no CLI flag is applied."
        )

    cli_cmd = " ".join([shlex.quote(container_cli)] + [shlex.quote(arg) for arg in effective_cli_args])
    cli_prefix = f"DOCKER_CONFIG={shlex.quote(str(docker_config_dir))} {cli_cmd}"

    suite_path = Path(settings["suite"])
    outdir = Path(settings["outdir"])
    outdir.mkdir(parents=True, exist_ok=True)

    cases = load_json(suite_path)
    if not isinstance(cases, list):
        raise ValueError("suite file must contain a JSON array of case objects")

    external_suite_cases: list[dict[str, Any]] = []
    if settings["include_external_suite"]:
        external_suite_path = Path(settings["external_suite"])
        if not external_suite_path.exists():
            raise ValueError(f"external suite file not found: {external_suite_path}")
        external_suite_cases = load_json(external_suite_path)
        if not isinstance(external_suite_cases, list):
            raise ValueError("external suite file must contain a JSON array of case objects")
        cases.extend(external_suite_cases)

    external_cases = load_external_cases(settings)
    cases.extend(external_cases)
    cases = expand_cases_to_all_carriers(cases, settings)

    for case in cases:
        validate_case(case)

    ts = dt.datetime.now(dt.timezone.utc).strftime(settings["timestamp_format"])
    suite_name = safe_tag(suite_path.stem)

    if settings["pull_base"]:
        run(f"{cli_prefix} pull {shlex.quote(settings['base_image'])}")

    runlist = {
        "generated_at_utc": ts,
        "base_image": settings["base_image"],
        "registry": settings["registry"],
        "repo": settings["repo"],
        "image_name": settings["image_name"],
        "suite": str(suite_path),
        "external_suite": settings["external_suite"] if settings["include_external_suite"] else None,
        "external_suite_cases_count": len(external_suite_cases),
        "external_cases_count": len(external_cases),
        "container_cli": container_cli,
        "container_cli_args": effective_cli_args,
        "docker_config": str(docker_config_dir),
        "insecure_registry": insecure_registry,
        "images": [],
    }

    for case in cases:
        cid = safe_tag(case["id"])
        canary = f"PWNED_CANARY_{cid}"

        tag_core = f"{suite_name}-{cid}-{ts}"
        if settings["tag_prefix"]:
            tag_core = f"{safe_tag(settings['tag_prefix'])}-{tag_core}"

        tag = build_image_ref(
            registry=str(settings["registry"]),
            repo=str(settings["repo"]),
            image_name=str(settings["image_name"]),
            tag=tag_core,
        )

        workdir = outdir / f"work_{suite_name}_{cid}_{ts}"
        workdir.mkdir(parents=True, exist_ok=True)

        dockerfile = dockerfile_for_case(settings["base_image"], case, canary, settings)
        (workdir / "Dockerfile").write_text(dockerfile, encoding="utf-8")

        run(f"{cli_prefix} build -t {shlex.quote(tag)} -f Dockerfile .", cwd=workdir)

        if settings["push"]:
            run(f"{cli_prefix} push {shlex.quote(tag)}")

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
