# container-ai-testing

Config-driven mutation harness for container image LLM security testing.

## Structure

- `config/build_push.config.json`: all reusable variables/parameters
- `cases/suite_basic.json`: test-case catalog (carrier + payload + optional path)
- `tools/build_push.py`: mutate -> build -> push -> runlist generator
- `out/`: generated Dockerfiles and runlists

## Configure

Edit `config/build_push.config.json`:

- `base_image`: source image to mutate
- `registry`: target registry host:port
- `repo`: repository name in registry
- `suite`: path to cases JSON
- `outdir`: output folder
- `push`: whether to push after build
- `pull_base`: pull base image before builds
- `tag_prefix`: optional prefix for tags
- `timestamp_format`: UTC timestamp format for tags/runlist

## Run

Use only config values:

```bash
python3 tools/build_push.py
```

Override specific values when needed:

```bash
python3 tools/build_push.py \
  --config config/build_push.config.json \
  --base-image your/base:image-tag \
  --registry localhost:5001 \
  --repo llmsec \
  --suite cases/suite_basic.json \
  --push
```

Output:

- `out/runlist_<suite>_<ts>.json` with:
  - image tag
  - canary
  - carrier
  - path (if file carrier)
  - payload preview
