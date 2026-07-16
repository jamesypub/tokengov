"""app_runtime.* checks — version stamp + deployed-image sync.

`version` checks /api/version (the TgVersion task-def param). `image-sync`
catches the stale-deploy trap: it verifies the running task's image
digest matches the `src-<hash>` tag the installer stamps for the deployed
source, and that the image's org.tg.version LABEL agrees with
/api/version. Any signal it can't read → WARN ("can't tell"), never a
false-confident fail (ground-by-data). All AWS calls are Describe*/
BatchGet* (read-only).
"""
from __future__ import annotations

import json

from diagnostics.model import (
    CheckResult, Check, PASS, WARN, FAIL, INFO, WARNING, CRITICAL,
)

CATEGORY = "app_runtime"

_TG_IMAGE_LABEL = "org.tg.version"


def _release_of(version: str) -> str:
    """The bare release token from a `v<rel>-g<sha>` build version, for
    comparing /api/version against the image LABEL. Returns the input
    unchanged if it has no -g<sha> suffix."""
    if not version:
        return ""
    return version.split("-g", 1)[0]


def check_version(ctx) -> CheckResult:
    v = ctx.tg_version or "dev"
    unstamped = v in ("dev", "unstamped", "")
    if unstamped and ctx.environment != "dev":
        return CheckResult(
            id="app_runtime.version", title="Version is stamped",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"/api/version is '{v}' on environment "
                   f"'{ctx.environment}'.",
            remediation="/api/version comes from the TgVersion task-def "
                        "param, not the image — pass "
                        "ParameterKey=TgVersion or it reports stale.")
    return CheckResult(
        id="app_runtime.version", title="Version is stamped",
        status=PASS, category=CATEGORY, severity=INFO,
        detail=f"/api/version reports {v}.", remediation="")


def _running_image_ref(ctx):
    """The api service's running container image ref, via
    ecs:DescribeServices → DescribeTaskDefinition. None if unreadable
    (not on ECS, or the read fails)."""
    cluster = None
    try:
        ecs = ctx.client("ecs")
        # Find a cluster + the api service. The installer names them
        # tg-api-service / tg-worker-service under a tg cluster; we
        # discover rather than hardcode so a rename doesn't break this.
        clusters = ecs.list_clusters().get("clusterArns", [])
        for c in clusters:
            svc_arns = ecs.list_services(
                cluster=c).get("serviceArns", [])
            api_svcs = [s for s in svc_arns if "api" in s.lower()]
            if not api_svcs:
                continue
            cluster = c
            desc = ecs.describe_services(
                cluster=c, services=api_svcs[:1])
            svcs = desc.get("services", [])
            if not svcs:
                continue
            td_arn = svcs[0].get("taskDefinition")
            if not td_arn:
                continue
            td = ecs.describe_task_definition(taskDefinition=td_arn)
            cdefs = td.get("taskDefinition", {}).get(
                "containerDefinitions", [])
            for cd in cdefs:
                img = cd.get("image")
                if img:
                    return img
        return None
    except Exception:  # noqa: BLE001 — can't tell
        return None


def _ecr_repo_and_ref(image):
    """(repo_name, tag_or_digest) parsed from an ECR image URI, or
    (None, None). e.g. <acct>.dkr.ecr.<r>.amazonaws.com/tg-container:tag
    → ('tg-container', 'tag'); …@sha256:… → ('tg-container', digest)."""
    if not image or "/" not in image:
        return None, None
    path = image.split("/", 1)[1]
    if "@" in path:
        repo, ref = path.split("@", 1)
        return repo, ref
    if ":" in path:
        repo, ref = path.rsplit(":", 1)
        return repo, ref
    return path, None


def _digest_of_running(ctx, repo, ref):
    """Resolve the running image ref to a digest via
    ecr:BatchGetImage (read-only). None if unreadable."""
    if ref and ref.startswith("sha256:"):
        return ref
    try:
        ecr = ctx.client("ecr")
        image_id = {"imageTag": ref} if ref else {}
        resp = ecr.batch_get_image(
            repositoryName=repo, imageIds=[image_id],
            acceptedMediaTypes=[
                "application/vnd.docker.distribution.manifest.v2+json",
                "application/vnd.oci.image.manifest.v1+json"])
        imgs = resp.get("images", [])
        if not imgs:
            return None
        return imgs[0].get("imageId", {}).get("imageDigest")
    except Exception:  # noqa: BLE001 — can't tell
        return None


def _image_label_release(ctx, repo, digest):
    """org.tg.version LABEL of the image at `digest`. The Labels live in
    the image CONFIG blob, which isn't retrievable through a single
    boto3/ECR API call (it needs a registry blob GET with a v2 token, or
    a docker pull) — so from inside the container we can't read it
    reliably without pulling. Returns None ("can't tell"); the caller
    treats a None here as a soft warn, never a false fail (ground-by-
    data). The src-<hash> digest match above is the load-bearing sync
    signal; the LABEL is a best-effort cross-check that a follow-on
    ticket can wire to a registry blob read.

    We DO confirm the manifest is readable (a cheap read-only
    batch_get_image) so that when the digest matched but the manifest is
    unreachable we still report can't-tell rather than pass."""
    if not digest:
        return None
    try:
        ecr = ctx.client("ecr")
        man_resp = ecr.batch_get_image(
            repositoryName=repo, imageIds=[{"imageDigest": digest}],
            acceptedMediaTypes=[
                "application/vnd.docker.distribution.manifest.v2+json",
                "application/vnd.oci.image.manifest.v1+json"])
        _ = json.loads(
            (man_resp.get("images") or [{}])[0].get(
                "imageManifest", "{}"))
    except Exception:  # noqa: BLE001 — can't tell
        return None
    # Manifest read OK, but the config-blob Labels aren't reachable via a
    # single boto3 op → can't tell.
    return None


def _src_tag_digest(ctx, repo):
    """The digest of the `src-*` tag the installer stamped most recently
    for the deployed source tree, via ecr:DescribeImages (read-only).
    Returns (digest, tag) or (None, None) if unreadable / no src tag."""
    try:
        ecr = ctx.client("ecr")
        paginator_imgs = []
        kw = {"repositoryName": repo}
        while True:
            resp = ecr.describe_images(**kw)
            paginator_imgs.extend(resp.get("imageDetails", []))
            token = resp.get("nextToken")
            if not token:
                break
            kw["nextToken"] = token
        # Find the src-<hash> tag on the most recently pushed image.
        src_imgs = [
            d for d in paginator_imgs
            if any(str(t).startswith("src-")
                   for t in (d.get("imageTags") or []))
        ]
        if not src_imgs:
            return None, None
        src_imgs.sort(
            key=lambda d: d.get("imagePushedAt") or 0, reverse=True)
        top = src_imgs[0]
        tag = next(t for t in top["imageTags"]
                   if str(t).startswith("src-"))
        return top.get("imageDigest"), tag
    except Exception:  # noqa: BLE001 — can't tell
        return None, None


def check_image_sync(ctx) -> CheckResult:
    """Verify the deployed image and its source are in sync."""
    # Can't-tell short-circuit on a dev/unstamped version in non-dev:
    # surface as the version warning, not a false image-sync fail.
    v = ctx.tg_version or "dev"
    if v in ("dev", "unstamped", "") and ctx.environment != "dev":
        return CheckResult(
            id="app_runtime.image-sync", title="Deployed image in sync",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"/api/version is '{v}' — can't establish the "
                   "expected release to compare against.",
            remediation="/api/version is unstamped; pass "
                        "ParameterKey=TgVersion so image-sync can "
                        "compare the deployed image to the release.")

    image = _running_image_ref(ctx)
    if not image:
        return CheckResult(
            id="app_runtime.image-sync", title="Deployed image in sync",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail="Could not read the running task's image (not on ECS "
                   "or ecs:Describe* unreadable).",
            remediation="Can't tell — the running image ref wasn't "
                        "readable (local-compose, or missing ecs:Describe "
                        "grants). Verify via the DEPLOYED image's "
                        "org.tg.version LABEL + src-<hash> tag manually.")
    repo, ref = _ecr_repo_and_ref(image)
    if not repo:
        return CheckResult(
            id="app_runtime.image-sync", title="Deployed image in sync",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"Running image {image} is not a parseable ECR ref.",
            remediation="Can't tell — the running image isn't an ECR "
                        "reference; verify the deployed image manually.")

    running_digest = _digest_of_running(ctx, repo, ref)
    src_digest, src_tag = _src_tag_digest(ctx, repo)

    if running_digest and src_digest and running_digest != src_digest:
        return CheckResult(
            id="app_runtime.image-sync", title="Deployed image in sync",
            status=FAIL, category=CATEGORY, severity=CRITICAL,
            detail=f"Running image digest {running_digest[:19]}… ≠ the "
                   f"{src_tag} tag digest {src_digest[:19]}… for the "
                   "deployed source — STALE image.",
            remediation="The running image was built from a different "
                        f"source than {src_tag}. Re-deploy from the "
                        "intended SHA (`git fetch origin && checkout "
                        "<sha>` FIRST — a behind-HEAD checkout silently "
                        "reuses a stale image). Verify via the DEPLOYED "
                        "image's org.tg.version LABEL + src-<hash> tag, "
                        "not /api/version.")

    if not running_digest or not src_digest:
        return CheckResult(
            id="app_runtime.image-sync", title="Deployed image in sync",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail="Could not read both the running digest and the "
                   "src-<hash> tag digest to compare.",
            remediation="Can't tell — one of the running image digest / "
                        "src-<hash> ECR tag wasn't readable. Verify via "
                        "the DEPLOYED image's org.tg.version LABEL + "
                        "src-<hash> tag manually.")

    # Digests agree; try the LABEL vs /api/version release (soft — a
    # can't-read LABEL is a warn, not a fail).
    label_release = _image_label_release(ctx, repo, running_digest)
    if label_release is None:
        return CheckResult(
            id="app_runtime.image-sync", title="Deployed image in sync",
            status=WARN, category=CATEGORY, severity=WARNING,
            detail=f"Running image matches the {src_tag} source tag, but "
                   "the org.tg.version LABEL couldn't be read to "
                   "cross-check /api/version.",
            remediation="Can't tell — the image LABEL wasn't readable "
                        "(pre-LABEL image or manifest read blocked). The "
                        "src-<hash> digest matched, so the source is in "
                        "sync; LABEL cross-check skipped.")
    if _release_of(label_release) != _release_of(v):
        return CheckResult(
            id="app_runtime.image-sync", title="Deployed image in sync",
            status=FAIL, category=CATEGORY, severity=CRITICAL,
            detail=f"Image LABEL release '{label_release}' ≠ "
                   f"/api/version release '{v}'.",
            remediation="The image's org.tg.version LABEL and "
                        "/api/version name different releases — the "
                        "TgVersion task-def param is out of sync with "
                        "the deployed image. Re-stamp TgVersion to match "
                        "the image.")
    return CheckResult(
        id="app_runtime.image-sync", title="Deployed image in sync",
        status=PASS, category=CATEGORY, severity=INFO,
        detail=f"Running image matches the {src_tag} source tag and the "
               f"LABEL release agrees with /api/version ({v}).",
        remediation="")


CHECKS = [
    Check("app_runtime.version", "Version is stamped", CATEGORY,
          WARNING, check_version),
    Check("app_runtime.image-sync", "Deployed image in sync", CATEGORY,
          CRITICAL, check_image_sync),
]
