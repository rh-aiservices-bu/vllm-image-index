#!/usr/bin/env python3
"""
fetch.py — Query Red Hat and DockerHub registries for vLLM images.
Outputs data.json and data.js in --output-dir (default: current directory).

Usage:
  python3 fetch.py                         # full run
  python3 fetch.py --skip-models           # skip model-named preview images
  python3 fetch.py --auth-file /mnt/auth/.dockerconfigjson --output-dir /tmp/workdir
"""
import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument("--skip-models", action="store_true", help="Skip model-named preview images")
parser.add_argument("--auth-file", default=os.path.expanduser("~/.config/containers/auth.json"),
                    help="Path to container auth JSON (default: ~/.config/containers/auth.json)")
parser.add_argument("--output-dir", default=".",
                    help="Directory to write data.json and data.js (default: current directory)")
args = parser.parse_args()
SKIP_MODELS = args.skip_models

AUTH_FILE = args.auth_file
_OUTPUT_DIR = args.output_dir
RH_REGISTRY = "registry.redhat.io"
PYPI_BASE = "https://packages.redhat.com/api/pypi/public-rhai/rhoai"

# (namespace, source_label, repos)
RH_NAMESPACES = [
    ("rhaiis", "Red Hat (RHAI)", [
        "vllm-cuda-rhel9", "vllm-rocm-rhel9", "vllm-cpu-rhel9",
        "vllm-tpu-rhel9", "vllm-neuron-rhel9", "vllm-spyre-rhel9",
    ]),
    ("rhaii", "Red Hat (RHAI)", [
        "vllm-cuda-rhel9", "vllm-rocm-rhel9", "vllm-cpu-rhel9",
        "vllm-tpu-rhel9", "vllm-neuron-rhel9", "vllm-spyre-rhel9",
        "vllm-gaudi-rhel9",
    ]),
    ("rhaiis-preview", "Red Hat (RHAI Preview)", [
        "vllm-cuda-rhel9",
    ]),
    ("rhaii-preview", "Red Hat (RHAI Preview)", [
        "vllm-cuda-rhel9",
    ]),
    ("rhaii-early-access", "Red Hat (RHAI Early Access)", [
        "vllm-cuda-rhel9", "vllm-rocm-rhel9", "vllm-cpu-rhel9",
        "vllm-tpu-rhel9", "vllm-neuron-rhel9", "vllm-spyre-rhel9",
        "vllm-gaudi-rhel9",
    ]),
]

# (repo, hardware, source_label)
DOCKERHUB_REPOS = [
    ("vllm/vllm-openai",      "cuda", "Upstream vLLM (DockerHub)"),
    ("vllm/vllm-openai-cpu",  "cpu",  "Upstream vLLM (DockerHub)"),
    ("vllm/vllm-openai-rocm", "rocm", "Upstream vLLM (DockerHub)"),
    ("vllm/vllm-openai-xpu",  "xpu",  "Upstream vLLM (DockerHub)"),
    # vllm/vllm-tpu is handled separately — emits both "tpu" and "ironwood" hardware
]


def skopeo_list_tags(image):
    result = subprocess.run(
        ["skopeo", "list-tags", f"--authfile={AUTH_FILE}", f"docker://{image}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  WARN {image}: {result.stderr.strip()}", file=sys.stderr)
        return []
    return json.loads(result.stdout).get("Tags", [])


def skopeo_inspect(image_ref):
    """Return the full skopeo inspect dict (not just labels)."""
    result = subprocess.run(
        [
            "skopeo", "inspect",
            "--override-arch", "amd64", "--override-os", "linux",
            f"--authfile={AUTH_FILE}",
            f"docker://{image_ref}",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  WARN inspect {image_ref}: {result.stderr.strip()}", file=sys.stderr)
        return {}
    return json.loads(result.stdout)


def skopeo_inspect_labels(image_ref):
    return skopeo_inspect(image_ref).get("Labels", {})


def fetch_pypi_vllm_version(url):
    """Query a PyPI simple index page and extract the base vLLM version from a wheel filename."""
    result = subprocess.run(["curl", "-sf", url], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    m = re.search(r'vllm-([\d.]+(?:rc\d+)?)[+%]', result.stdout)
    return m.group(1) if m else None


def hardware_from_repo(repo_name):
    m = re.search(r"vllm-(\w+)-rhel9", repo_name)
    return m.group(1) if m else "unknown"


def rh_tag_excluded(tag):
    return (
        tag.startswith("sha256-")
        or tag.endswith("-source")
        or tag in ("latest",)
        or bool(re.fullmatch(r"\d+", tag))
        or bool(re.search(r"-\d{7,}", tag))
    )


def is_semver(tag):
    return bool(re.fullmatch(r"\d+(\.\d+)+", tag))


def dockerhub_tags(repo):
    return skopeo_list_tags(f"docker.io/{repo}")


def dh_tag_included(tag):
    if not re.match(r"^v\d+\.\d+\.\d+", tag):
        return False
    if re.search(r"-(x86_64|aarch64|arm64|base)\b", tag):
        return False
    return True


def dh_version(tag):
    m = re.match(r"^v(\d+\.\d+\.\d+(?:\.\w+)?)", tag)
    return m.group(1) if m else tag


def semver_key(v):
    return [int(x) for x in re.split(r"[.\-]", v) if x.isdigit()]


EA_NAMESPACES = {"rhaii-early-access", "rhaiis-early-access"}

# Cache layer digest → vllm version (or None if not found) to avoid re-downloading
# layers shared between image tags (e.g. floating minor tag and its latest patch).
_layer_version_cache = {}

# Cache RH first-layer digest → OS name (shared base layers downloaded at most once per run).
_rh_os_layer_cache = {}


def normalise_os_name(name, version_id):
    """Produce a short, consistent OS name from /etc/os-release NAME + VERSION_ID fields."""
    if not name or not version_id:
        return None
    n = name.strip().lower()
    v = version_id.strip()
    if "red hat enterprise linux" in n or n == "rhel":
        return f"RHEL {v}"
    if "ubuntu" in n:
        return f"Ubuntu {v}"
    if "debian" in n:
        return f"Debian {v}"
    return f"{name.strip()} {v}"

# Cache DockerHub first-layer digest → OS name to avoid re-downloading shared base layers.
_dh_os_layer_cache = {}


def resolve_index_url(labels, minor, hw):
    index_url = labels.get("com.redhat.aiplatform.index_url", "")
    cuda_version = labels.get("com.redhat.aiplatform.cuda_version", "")
    index_version = labels.get("com.redhat.aiplatform.index_version", minor)

    cuda_short = ""
    if cuda_version:
        cv_parts = cuda_version.split(".")
        cuda_short = f"{cv_parts[0]}.{cv_parts[1]}" if len(cv_parts) >= 2 else cuda_version

    if index_url and "${" not in index_url:
        return index_url.rstrip("/") + "/vllm/"

    if index_url and "${" in index_url:
        variant = f"cuda{cuda_short}-ubi9" if cuda_short else hw + "-ubi9"
        resolved = index_url.replace("${INDEX_VERSION}", index_version).replace("${INDEX_VARIANT}", variant)
        return resolved.rstrip("/") + "/vllm/"

    return None


def variant_from_wheel_release(wheel_release):
    wr = wheel_release.split()[0] if wheel_release else ""
    m = re.search(r'\+rhaii[is]*-(.+?)(?:-(?:x86_64|aarch64|s390x|ppc64le))+$', wr)
    return m.group(1) if m else ""


def build_fallback_urls(minor, hw, cuda_short, wheel_release=""):
    urls = []
    variant = variant_from_wheel_release(wheel_release)
    if variant:
        urls.append(f"{PYPI_BASE}/{minor}/{variant}/simple/vllm/")
    if hw == "cuda" and cuda_short:
        urls += [
            f"{PYPI_BASE}/{minor}/cuda{cuda_short}-ubi9/simple/vllm/",
            f"{PYPI_BASE}/{minor}/cuda{cuda_short}/simple/vllm/",
        ]
    else:
        urls += [
            f"{PYPI_BASE}/{minor}/{hw}-ubi9/simple/vllm/",
            f"{PYPI_BASE}/{minor}/{hw}/simple/vllm/",
        ]
    return urls


def layer_inspect(repo, tag):
    """
    Inspect RH image layers to get both vllm_version and os_name in one auth session.
    - os_name: extracted from /etc/os-release in the first (base) layer, cached by digest.
    - vllm_version: found by streaming layers in reverse and grepping tar listings for
      the vllm dist-info directory name.
    Returns (vllm_version, os_name) — either may be None.
    """
    with open(AUTH_FILE) as f:
        auths = json.load(f).get("auths", {})
    raw_cred = next((v["auth"] for h, v in auths.items() if "redhat.io" in h), None)
    if not raw_cred:
        return None, None
    user_pass = base64.b64decode(raw_cred).decode()

    scope = f"repository:{repo}:pull"
    token_url = (
        f"https://{RH_REGISTRY}/auth/realms/rhcc/protocol/redhat-docker-v2/auth"
        f"?service=docker-registry&scope={scope}"
    )
    r = subprocess.run(["curl", "-sf", "-u", user_pass, token_url], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None, None
    d = json.loads(r.stdout)
    token = d.get("token") or d.get("access_token")
    if not token:
        return None, None

    def api_get(path, accept):
        r = subprocess.run(
            ["curl", "-sfL",
             "-H", f"Authorization: Bearer {token}",
             "-H", f"Accept: {accept}",
             f"https://{RH_REGISTRY}/v2/{path}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        try:
            return json.loads(r.stdout)
        except Exception:
            return {}

    ml = api_get(
        f"{repo}/manifests/{tag}",
        "application/vnd.oci.image.index.v1+json,"
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
    if "manifests" in ml:
        amd64_digest = next(
            (m["digest"] for m in ml["manifests"]
             if m.get("platform", {}).get("architecture") == "amd64"),
            None,
        )
        if not amd64_digest:
            return None, None
        img = api_get(
            f"{repo}/manifests/{amd64_digest}",
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    else:
        img = ml

    layers = img.get("layers", [])
    if not layers:
        return None, None

    # Extract OS from the first (base) layer — cached by layer digest so shared
    # base layers (same RHEL minor version) are only downloaded once per run.
    os_name = None
    first_digest = layers[0]["digest"]
    if first_digest in _rh_os_layer_cache:
        os_name = _rh_os_layer_cache[first_digest]
    else:
        blob_url = f"https://{RH_REGISTRY}/v2/{repo}/blobs/{first_digest}"
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpfile = os.path.join(tmpdir, "layer.tar.gz")
                r2 = subprocess.run(
                    ["curl", "-sfL", "-H", f"Authorization: Bearer {token}",
                     blob_url, "-o", tmpfile],
                    capture_output=True, timeout=180,
                )
                if r2.returncode == 0:
                    subprocess.run(
                        ["tar", "-xzf", tmpfile, "-C", tmpdir,
                         "--exclude=./proc", "--exclude=./sys"],
                        capture_output=True, timeout=60,
                    )
                    os_release = os.path.join(tmpdir, "etc", "os-release")
                    if os.path.exists(os_release):
                        with open(os_release) as f2:
                            content = f2.read()
                        name_m = re.search(r'^NAME="?([^"\n]+)"?', content, re.M)
                        ver_m = re.search(r'^VERSION_ID="?([^"\n]+)"?', content, re.M)
                        if name_m and ver_m:
                            os_name = normalise_os_name(name_m.group(1), ver_m.group(1))
        except Exception:
            pass
        _rh_os_layer_cache[first_digest] = os_name

    # Search layers in reverse (newest first) for the vllm dist-info directory.
    vllm_version = None
    for layer in reversed(layers):
        digest = layer["digest"]
        if digest in _layer_version_cache:
            cached = _layer_version_cache[digest]
            if cached:
                vllm_version = cached
                break
            continue
        blob_url = f"https://{RH_REGISTRY}/v2/{repo}/blobs/{digest}"
        shell_cmd = (
            f"curl -sfL -H 'Authorization: Bearer {token}' '{blob_url}' "
            f"| tar -tz 2>/dev/null "
            f"| grep 'vllm-[0-9].*\\.dist-info' "
            f"| head -1"
        )
        try:
            r = subprocess.run(
                shell_cmd, shell=True, capture_output=True, text=True, timeout=600,
            )
        except subprocess.TimeoutExpired:
            print(f"    layer {digest[7:20]}... timed out", file=sys.stderr)
            _layer_version_cache[digest] = None
            continue
        if r.stdout.strip():
            # Strip local version suffix (e.g. +rhai11) — dist-info can be
            # named "vllm-0.13.0+rhai11.dist-info" but we want just "0.13.0"
            m = re.search(r'vllm-(\d[\d.]+(?:rc\d+)?)(?:[+\-]|\.dist)', r.stdout)
            if m:
                _layer_version_cache[digest] = m.group(1)
                vllm_version = m.group(1)
                break
        _layer_version_cache[digest] = None

    return vllm_version, os_name


def dh_layer_os(repo, tag):
    """
    Get the manifest index digest and OS name for a DockerHub image.
    - Index digest: from Docker-Content-Digest response header (the manifest list digest,
      or single-manifest digest if no manifest list exists). This is the correct digest
      for pinning an image regardless of architecture.
    - OS name: from /etc/os-release in the first layer (cached by layer digest).
    Returns {"os_name": str|None, "digest": str|None}.
    """
    r = subprocess.run(
        ["curl", "-sf",
         f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return {"os_name": None, "digest": None}
    token = json.loads(r.stdout).get("token")
    if not token:
        return {"os_name": None, "digest": None}

    MANIFEST_ACCEPT = (
        "application/vnd.oci.image.index.v1+json,"
        "application/vnd.docker.distribution.manifest.list.v2+json,"
        "application/vnd.oci.image.manifest.v1+json,"
        "application/vnd.docker.distribution.manifest.v2+json"
    )

    # HEAD request to get the index digest from the response header
    head_r = subprocess.run(
        ["curl", "-sfI",
         "-H", f"Authorization: Bearer {token}",
         "-H", f"Accept: {MANIFEST_ACCEPT}",
         f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"],
        capture_output=True, text=True,
    )
    index_digest = None
    if head_r.returncode == 0:
        m = re.search(r'docker-content-digest:\s*(sha256:\S+)', head_r.stdout, re.I)
        if m:
            index_digest = m.group(1).strip()

    def dh_api(path, accept):
        r = subprocess.run(
            ["curl", "-sfL",
             "-H", f"Authorization: Bearer {token}",
             "-H", f"Accept: {accept}",
             f"https://registry-1.docker.io/v2/{path}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        try:
            return json.loads(r.stdout)
        except Exception:
            return {}

    ml = dh_api(
        f"{repo}/manifests/{tag}",
        "application/vnd.oci.image.index.v1+json,"
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
    if "manifests" in ml:
        amd64_digest = next(
            (m["digest"] for m in ml["manifests"]
             if m.get("platform", {}).get("architecture") == "amd64"),
            None,
        )
        if not amd64_digest:
            return {"os_name": None, "digest": index_digest}
        img = dh_api(
            f"{repo}/manifests/{amd64_digest}",
            "application/vnd.oci.image.manifest.v1+json,"
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    else:
        img = ml

    layers = img.get("layers", [])
    if not layers:
        return {"os_name": None, "digest": index_digest}

    first_digest = layers[0]["digest"]
    if first_digest in _dh_os_layer_cache:
        return {"os_name": _dh_os_layer_cache[first_digest], "digest": index_digest}

    blob_url = f"https://registry-1.docker.io/v2/{repo}/blobs/{first_digest}"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpfile = os.path.join(tmpdir, "layer.tar.gz")
            r = subprocess.run(
                ["curl", "-sfL", "-H", f"Authorization: Bearer {token}", blob_url, "-o", tmpfile],
                capture_output=True, timeout=120,
            )
            if r.returncode != 0:
                _dh_os_layer_cache[first_digest] = None
                return {"os_name": None, "digest": index_digest}
            subprocess.run(
                ["tar", "-xzf", tmpfile, "-C", tmpdir],
                capture_output=True, timeout=60,
            )
            os_release = os.path.join(tmpdir, "etc", "os-release")
            if not os.path.exists(os_release):
                _dh_os_layer_cache[first_digest] = None
                return {"os_name": None, "digest": index_digest}
            with open(os_release) as f:
                content = f.read()
    except Exception:
        _dh_os_layer_cache[first_digest] = None
        return {"os_name": None, "digest": index_digest}

    name_m = re.search(r'^NAME="?([^"\n]+)"?', content, re.M)
    ver_m = re.search(r'^VERSION_ID="?([^"\n]+)"?', content, re.M)
    os_name = normalise_os_name(name_m.group(1), ver_m.group(1)) if name_m and ver_m else None
    _dh_os_layer_cache[first_digest] = os_name
    return {"os_name": os_name, "digest": index_digest}


def resolve_dh_os(images, seed_prior=None):
    """
    Resolve OS name and index digest for DockerHub images via layer/manifest inspection.
    Caches non-null results in data.json; null entries are retried on restart.
    seed_prior: pre-loaded {pull: {os_name, digest}} from the original seeded data.json,
    provided before Phase 2 overwrites it with null DockerHub entries.
    """
    targets = [img for img in images if img["registry"] == "docker.io"]
    if not targets:
        return

    # Start with seed (captured before Phase 2 overwrote data.json).
    prior = dict(seed_prior) if seed_prior else {}

    # Merge in any entries from the current data.json (covers new images added since seed).
    if os.path.exists(os.path.join(_OUTPUT_DIR, "data.json")):
        try:
            with open(os.path.join(_OUTPUT_DIR, "data.json")) as f:
                existing = json.load(f)
            added = 0
            for img in existing.get("images", []):
                pull = img.get("pull")
                if pull and pull not in prior and (img.get("os_name") is not None or img.get("digest") is not None):
                    prior[pull] = {
                        "os_name": img.get("os_name"),
                        "digest": img.get("digest"),
                    }
                    added += 1
            if added:
                print(f"  Merged {added} additional OS/digest entries from data.json", file=sys.stderr)
        except Exception:
            pass
    if prior:
        print(f"  DockerHub cache: {len(prior)} entries total", file=sys.stderr)

    print("\nResolving DockerHub OS versions and digests ...", file=sys.stderr)
    seen = set()
    for img in targets:
        pull = img["pull"]
        if pull in seen:
            continue
        seen.add(pull)

        if pull in prior:
            entry = prior[pull]
            print(f"  {pull} → {entry.get('os_name')} (cached)", file=sys.stderr)
        else:
            print(f"  {pull} ...", file=sys.stderr)
            entry = dh_layer_os(img["repository"], img["tag"])
            print(f"    → {entry.get('os_name')} / {entry.get('digest', '')[:19]}...", file=sys.stderr)
            if entry.get("os_name") is not None or entry.get("digest") is not None:
                prior[pull] = entry

        for i in images:
            if i["pull"] == pull:
                i["os_name"] = entry.get("os_name")
                i["digest"] = entry.get("digest")

        write_output(images)


def write_output(images):
    """Write data.json."""
    output = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "images": images,
    }
    with open(os.path.join(_OUTPUT_DIR, "data.json"), "w") as f:
        json.dump(output, f, indent=2)


def resolve_vllm_mapping(images):
    """
    For each unique (version, hardware) combination across rhai/ea/model images, resolve
    the upstream vLLM version. Updates images in-place and writes data.json after
    each combo so progress survives interruption.

    On restart: non-null vllm_version entries in data.json are reused; null entries
    are retried (simple rule — no separate cache file needed).
    """
    combos = {}
    for img in images:
        if img["version_scheme"] in ("rhai", "ea") and img["version"]:
            key = f"{img['version']}:{img['hardware']}"
            combos[key] = img
        elif img["version_scheme"] == "model" and not SKIP_MODELS:
            # model images are unique per pull ref — no deduplication
            combos[img["pull"]] = img

    # Seed from previous data.json. Only trust entries that have os_name populated
    # (indicates they were inspected with the current schema). This forces re-inspection
    # on the first run after os_name was added; subsequent runs use the cache normally.
    prior = {}
    if os.path.exists(os.path.join(_OUTPUT_DIR, "data.json")):
        try:
            with open(os.path.join(_OUTPUT_DIR, "data.json")) as f:
                existing = json.load(f)
            for img in existing.get("images", []):
                pull = img.get("pull")
                if pull and img.get("vllm_version") is not None and "digest" in img:
                    prior[pull] = {
                        "vllm_version": img["vllm_version"],
                        "build_date": img.get("build_date"),
                        "cuda_version": img.get("cuda_version"),
                        "os_name": img.get("os_name"),
                        "digest": img.get("digest"),
                    }
            if prior:
                print(f"  Loaded {len(prior)} resolved entries from data.json", file=sys.stderr)
        except Exception:
            pass

    print("\nResolving vLLM version mapping ...", file=sys.stderr)

    for combo_key in sorted(combos):
        img_entry = combos[combo_key]
        hw = img_entry["hardware"]
        display_name = img_entry["version"] or img_entry["tag"]
        image_ref = img_entry["pull"]

        if image_ref in prior:
            print(f"  {image_ref} (cached)", file=sys.stderr)
            entry = prior[image_ref]
        else:
            print(f"  Inspecting {image_ref} ...", file=sys.stderr)
            raw = skopeo_inspect(image_ref)
            labels = raw.get("Labels", {})
            digest = raw.get("Digest")

            cuda_version = labels.get("com.redhat.aiplatform.cuda_version", "") or None
            build_date = labels.get("build-date") or None

            cuda_short = ""
            if cuda_version:
                cv_parts = cuda_version.split(".")
                cuda_short = f"{cv_parts[0]}.{cv_parts[1]}" if len(cv_parts) >= 2 else cuda_version

            os_name = None
            base_image = labels.get("com.redhat.aiplatform.base_image", "")
            if base_image:
                m = re.search(r':(\d+\.\d+)-\d+', base_image)
                if m:
                    os_name = f"RHEL {m.group(1)}"

            # For rhai/ea, minor comes from the version tag; for model images, from labels.
            if img_entry["version"]:
                minor = ".".join(img_entry["version"].split(".")[:2])
            else:
                minor = (
                    labels.get("com.redhat.aiplatform.repo_version")
                    or labels.get("com.redhat.aiplatform.index_version", "")
                )

            vllm_version = None
            resolved_url = resolve_index_url(labels, minor, hw)
            if resolved_url:
                print(f"    index_url → {resolved_url}", file=sys.stderr)
                vllm_version = fetch_pypi_vllm_version(resolved_url)
                if vllm_version:
                    print(f"    {display_name} ({hw}) → vLLM {vllm_version}", file=sys.stderr)

            if not vllm_version:
                wheel_release = labels.get("com.redhat.aiplatform.wheel_release", "")
                for fallback_url in build_fallback_urls(minor, hw, cuda_short, wheel_release):
                    print(f"    wheel_release fallback → {fallback_url}", file=sys.stderr)
                    vllm_version = fetch_pypi_vllm_version(fallback_url)
                    if vllm_version:
                        print(f"    {display_name} ({hw}) → vLLM {vllm_version} (pypi)", file=sys.stderr)
                        break

            if not vllm_version and img_entry["registry"] == RH_REGISTRY:
                print(f"    Falling back to layer inspection ...", file=sys.stderr)
                layer_vllm, layer_os = layer_inspect(img_entry["repository"], img_entry["tag"])
                if layer_vllm:
                    vllm_version = layer_vllm
                    print(f"    {display_name} ({hw}) → vLLM {vllm_version} (layer)", file=sys.stderr)
                else:
                    print(f"    {display_name} ({hw}) → vLLM unknown", file=sys.stderr)
                if layer_os and not os_name:
                    os_name = layer_os
                    print(f"    {display_name} ({hw}) → OS {os_name} (layer)", file=sys.stderr)

            entry = {
                "vllm_version": vllm_version,
                "build_date": build_date,
                "cuda_version": cuda_version,
                "os_name": os_name,
                "digest": digest,
            }
            if vllm_version is not None:
                prior[image_ref] = entry  # cache for within-run dedup

        # Update all matching images in-place
        if img_entry["version_scheme"] == "model":
            img_entry["vllm_version"] = entry["vllm_version"]
            img_entry["build_date"] = entry["build_date"]
            img_entry["cuda_version"] = entry["cuda_version"]
            img_entry["os_name"] = entry["os_name"]
            img_entry["digest"] = entry["digest"]
        else:
            for img in images:
                if img["version_scheme"] in ("rhai", "ea") and img["version"]:
                    if f"{img['version']}:{img['hardware']}" == combo_key:
                        img["vllm_version"] = entry["vllm_version"]
                        img["build_date"] = entry["build_date"]
                        img["cuda_version"] = entry["cuda_version"]
                        img["os_name"] = entry["os_name"]
                        img["digest"] = entry["digest"]

        # Write after every combo so an interrupted run has usable output
        write_output(images)


images = []

# ── Red Hat ───────────────────────────────────────────────────────────────────
for namespace, source_label, repos in RH_NAMESPACES:
    for repo in repos:
        full_repo = f"{namespace}/{repo}"
        image_ref = f"{RH_REGISTRY}/{full_repo}"
        print(f"Fetching {image_ref} ...", file=sys.stderr)
        tags = skopeo_list_tags(image_ref)
        hw = hardware_from_repo(repo)
        is_ea_ns = namespace in EA_NAMESPACES
        for tag in tags:
            if rh_tag_excluded(tag):
                continue
            if is_ea_ns:
                version_scheme = "ea"
            else:
                version_scheme = "rhai" if is_semver(tag) else "model"
            images.append({
                "registry": RH_REGISTRY,
                "repository": full_repo,
                "tag": tag,
                "version": tag if version_scheme in ("rhai", "ea") else None,
                "version_scheme": version_scheme,
                "hardware": hw,
                "source_label": source_label,
                "pull": f"{image_ref}:{tag}",
                "vllm_version": None,
                "build_date": None,
                "cuda_version": None,
                "os_name": None,
                "digest": None,
            })

# ── DockerHub ─────────────────────────────────────────────────────────────────
for repo, hw, source_label in DOCKERHUB_REPOS:
    print(f"Fetching docker.io/{repo} ...", file=sys.stderr)
    tags = dockerhub_tags(repo)
    for tag in tags:
        if not dh_tag_included(tag):
            continue
        images.append({
            "registry": "docker.io",
            "repository": repo,
            "tag": tag,
            "version": dh_version(tag),
            "version_scheme": "vllm",
            "hardware": hw,
            "source_label": source_label,
            "pull": f"docker.io/{repo}:{tag}",
            "vllm_version": None,
            "build_date": None,
            "cuda_version": None,
            "os_name": None,
            "digest": None,
        })

# ── DockerHub: TPU (two hardware variants — tpu and ironwood) ─────────────────
print("Fetching docker.io/vllm/vllm-tpu ...", file=sys.stderr)
for tag in dockerhub_tags("vllm/vllm-tpu"):
    if not re.match(r"^v\d+\.\d+\.\d+", tag):
        continue
    if re.search(r"-(x86_64|aarch64|arm64|base)\b", tag):
        continue
    is_ironwood = tag.endswith("-ironwood")
    hw = "ironwood" if is_ironwood else "tpu"
    base_tag = tag[: -len("-ironwood")] if is_ironwood else tag
    images.append({
        "registry": "docker.io",
        "repository": "vllm/vllm-tpu",
        "tag": tag,
        "version": dh_version(base_tag),
        "version_scheme": "vllm",
        "hardware": hw,
        "source_label": "Upstream vLLM (DockerHub)",
        "pull": f"docker.io/vllm/vllm-tpu:{tag}",
        "vllm_version": None,
        "build_date": None,
        "cuda_version": None,
        "os_name": None,
        "digest": None,
    })

# Pre-load DockerHub OS/digest cache from seeded data.json before Phase 2
# overwrites it with null DockerHub entries.
_dh_seed_prior = {}
if os.path.exists(os.path.join(_OUTPUT_DIR, "data.json")):
    try:
        with open(os.path.join(_OUTPUT_DIR, "data.json")) as f:
            _seed_data = json.load(f)
        for _img in _seed_data.get("images", []):
            if _img.get("registry") == "docker.io":
                _pull = _img.get("pull")
                if _pull and (_img.get("os_name") is not None or _img.get("digest") is not None):
                    _dh_seed_prior[_pull] = {
                        "os_name": _img.get("os_name"),
                        "digest": _img.get("digest"),
                    }
        if _dh_seed_prior:
            print(f"  Pre-loaded {len(_dh_seed_prior)} DockerHub cache entries from seed", file=sys.stderr)
    except Exception:
        pass

# ── Phase 2: resolve vLLM versions + OS for RH/EA/model images ───────────────
resolve_vllm_mapping(images)

# ── Phase 3: resolve OS for DockerHub images via layer inspection ─────────────
resolve_dh_os(images, _dh_seed_prior)

write_output(images)
print(f"\nDone — {len(images)} images written to data.json", file=sys.stderr)
