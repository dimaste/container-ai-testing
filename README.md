# container-ai-testing

Config-driven mutation harness for container image LLM security testing.

## Structure

- `config/build_push.config.json`: all reusable variables/parameters
- `cases/suite_basic.json`: base suite for manual edits (adapted from Promptfoo list, manually adjusted)
- `cases/suite_external.json`: external prompt suite generated from remote source(s)
- `cases/prompt_sources_promptfoo.json`: external prompt source manifest (Promptfoo CyberSecEval EN, `user_input` field)
- `tools/build_push.py`: mutate -> build -> push -> runlist generator
- `tools/refresh_suite.py`: refresh `suite_external.json` from external source(s)
- `third_party/promptfoo/prompt_source_references.md`: upstream Promptfoo plugin file references
- `out/`: generated Dockerfiles and runlists

## Configure

Edit `config/build_push.config.json`:

- `base_image`: source image to mutate
- `container_cli`: `docker` or `nerdctl`
- `container_cli_args`: extra CLI args, e.g. `["--namespace", "k8s.io"]` for nerdctl
- `insecure_registry`: if `true`, adds `--insecure-registry` for `nerdctl`; for Docker use daemon `insecure-registries`
- `registry`: target registry host:port
- `repo`: repository/group path in registry
- `image_name`: image name inside repository path
- `suite`: path to cases JSON
- `external_suite`: path to external cases JSON
- `include_external_suite`: include `external_suite` in build/push run
- `outdir`: output folder
- `push`: whether to push after build
- `pull_base`: pull base image before builds
- `tag_prefix`: optional prefix for tags
- `timestamp_format`: UTC timestamp format for tags/runlist
- `trace_labels_enabled`: add/remove internal trace labels (`case_id`, `canary`, `carrier`)
- `payload_label_key`: label key used for `label` carrier (default: `payload`)
- `payload_env_key`: env var key used for `env` carrier (default: `PAYLOAD`)
- `expand_case_to_all_carriers`: if `true`, each case is duplicated into all carriers
- `expand_carriers`: carrier list used by expansion
- `expand_file_path_template`: file path template used for expanded `file` cases
- `external_prompts_enabled`: include external prompts into generated test cases
- `external_prompt_manifest`: file with external sources (URL + parser settings)
- `external_prompts_limit`: cap for total external prompts
- `external_case_prefix`: prefix for generated external case ids
- `external_carrier_cycle`: carriers used for generated external cases
- `external_file_path_template`: file path template for generated `file` carrier cases
- `external_fetch_timeout_seconds`: timeout for fetching remote datasets

## Run

Use only config values:

```bash
python3 tools/build_push.py
```

Or from repo root:

```bash
./run_build_push.sh
```

With explicit config file:

```bash
./run_build_push.sh config/build_push.config.json
```

## External inputs

`suite_basic.json` is intended for manual curation.
Prompts there were adapted from the Promptfoo list and then manually edited.

Remote prompts should go into `suite_external.json` via refresh:

Refresh command:

```bash
python3 tools/refresh_suite.py
```

To connect another external file:

1. Edit/add source in `cases/prompt_sources_promptfoo.json`:
   - `format`: `json` or `csv`
   - `url`: raw file URL (prefer pinned commit/tag)
   - `field`: field name to use as payload (e.g. `user_input` or `prompt`)
   - or `template`: if payload must be composed from multiple fields
2. Refresh external suite:
   - `python3 tools/refresh_suite.py`
3. Ensure build config has:
   - `"include_external_suite": true`
   - `"external_suite": "cases/suite_external.json"`
4. Run `python3 tools/build_push.py`

Minimal source example:

```json
{
  "id": "my_source",
  "format": "csv",
  "url": "https://raw.githubusercontent.com/<org>/<repo>/<commit>/path/file.csv",
  "field": "prompt",
  "limit": 50,
  "shuffle": true
}
```

Use nerdctl from config:

```json
{
  "container_cli": "nerdctl",
  "container_cli_args": ["--namespace", "k8s.io"]
}
```

Override specific values when needed:

```bash
python3 tools/build_push.py \
  --config config/build_push.config.json \
  --container-cli nerdctl \
  --base-image your/base:image-tag \
  --registry localhost:5001 \
  --repo llmsec \
  --image-name llmsec-mutated \
  --suite cases/suite_basic.json \
  --push
```

Image reference format:

`<registry>/<repo>/<image_name>:<tag>`

Example:

`localhost:5001/llmsec/llmsec-mutated:suite_basic-label_01-20260218170000`

Output:

- `out/runlist_<suite>_<ts>.json` with:
  - image tag
  - canary
  - carrier
  - path (if file carrier)
  - payload preview
  - selected container tool (`docker` or `nerdctl`)
  - number of appended external cases
