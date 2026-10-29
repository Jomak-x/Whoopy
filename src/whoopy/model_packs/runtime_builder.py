"""Reproducible release builder for optional isolated Python runtimes.

The builder never packages an existing virtual environment.  It snapshots an
exact Git revision plus a complete, hash-locked, target-specific wheelhouse.
The normal runtime installer creates a fresh virtual environment from those
bytes with networking disabled.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from whoopy.model_packs.operations import ModelPackOperationError


class RuntimeBuildRecipe(BaseModel):
    """Reviewed inputs needed to create one runtime release bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    revision: str
    upstream_repository: str
    upstream_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    python_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    source_markers: tuple[Path, ...] = Field(min_length=1)
    import_modules: tuple[str, ...] = Field(min_length=1)
    supported_targets: tuple[str, ...] = Field(min_length=1)
    lock_paths: dict[str, Path] = Field(default_factory=dict)
    excluded_submodules: dict[Path, str] = Field(default_factory=dict)

    @field_validator("upstream_repository")
    @classmethod
    def _repository_is_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("upstream repository must use HTTPS")
        return value

    @field_validator("source_markers")
    @classmethod
    def _markers_are_relative(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(value.is_absolute() or ".." in value.parts for value in values):
            raise ValueError("source markers must be safe relative paths")
        return values

    @field_validator("import_modules")
    @classmethod
    def _imports_are_dotted_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", value) for value in values):
            raise ValueError("runtime import probes must be dotted Python identifiers")
        return values

    @field_validator("supported_targets")
    @classmethod
    def _targets_are_safe_and_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) for value in values):
            raise ValueError("runtime targets must be safe platform identifiers")
        if len(values) != len(set(values)):
            raise ValueError("runtime targets must be unique")
        return values

    @model_validator(mode="after")
    def _target_locks_are_declared(self) -> RuntimeBuildRecipe:
        unknown = set(self.lock_paths) - set(self.supported_targets)
        if unknown:
            raise ValueError(f"lock paths declare unsupported targets: {sorted(unknown)}")
        unsafe = [
            str(path)
            for path in self.lock_paths.values()
            if path.is_absolute() or ".." in path.parts or not path.parts
        ]
        if unsafe:
            raise ValueError(f"runtime lock paths must be safe relative paths: {unsafe}")
        return self

    @field_validator("excluded_submodules")
    @classmethod
    def _submodules_are_exact(cls, values: dict[Path, str]) -> dict[Path, str]:
        for path, revision in values.items():
            if (
                path.is_absolute()
                or ".." in path.parts
                or not re.fullmatch(r"[0-9a-f]{40}", revision)
            ):
                raise ValueError("excluded submodules require safe paths and exact Git revisions")
        return values


class RuntimeBuildRecipes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    runtimes: tuple[RuntimeBuildRecipe, ...]

    @model_validator(mode="after")
    def _runtime_ids_are_unique(self) -> RuntimeBuildRecipes:
        identifiers = [recipe.runtime_id for recipe in self.runtimes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("runtime build recipe IDs must be unique")
        return self


class RuntimeBundleBuildRecord(BaseModel):
    """Machine-readable provenance embedded in and emitted beside a bundle."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    runtime_id: str
    revision: str
    upstream_repository: str
    upstream_revision: str
    target: str
    python_version: str
    source_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirements_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    wheelhouse: dict[str, str]
    import_modules: tuple[str, ...]
    excluded_submodules: dict[str, str] = Field(default_factory=dict)


class RuntimeBundleBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    archive: Path
    filename: str
    size_bytes: int
    sha256: str
    target: str
    install_mode: Literal["locked-wheelhouse"] = "locked-wheelhouse"
    record: RuntimeBundleBuildRecord


def load_runtime_build_recipes(path: Path) -> RuntimeBuildRecipes:
    try:
        return RuntimeBuildRecipes.model_validate(yaml.safe_load(path.read_text("utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as error:
        raise ModelPackOperationError(f"Invalid runtime build recipes {path}: {error}") from error


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_hashed_requirements(payload: bytes, wheelhouse: Path) -> dict[str, str]:
    """Reject floating, online, editable, mismatched, or incomplete locks.

    A digest appearing somewhere in a lock is insufficient: each wheel must
    match the normalized distribution name and exact version of the requirement
    line that carries its digest.  This prevents a valid wheel digest from being
    moved onto an unrelated requirement while still passing bundle review.
    """

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ModelPackOperationError("Runtime requirements lock must be UTF-8.") from error
    logical: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        current = f"{current} {line}".strip()
        if current.endswith("\\"):
            current = current[:-1].strip()
            continue
        logical.append(current)
        current = ""
    if current:
        logical.append(current)
    if not logical:
        raise ModelPackOperationError("Runtime requirements lock is empty.")
    locked: dict[tuple[str, str], set[str]] = {}
    for requirement in logical:
        if requirement.startswith("-") or " @ " in requirement or "://" in requirement:
            raise ModelPackOperationError(
                f"Runtime lock contains an unsupported external directive: {requirement}"
            )
        tokens = requirement.split()
        package = tokens[0]
        if "==" not in package or package.startswith((".", "/")):
            raise ModelPackOperationError(
                f"Runtime dependency is not exactly pinned with ==: {requirement}"
            )
        try:
            parsed = Requirement(package)
        except InvalidRequirement as error:
            raise ModelPackOperationError(
                f"Runtime dependency is not a valid exact requirement: {requirement}"
            ) from error
        versions = [item.version for item in parsed.specifier if item.operator == "=="]
        if (
            len(parsed.specifier) != 1
            or len(versions) != 1
            or parsed.url is not None
            or parsed.marker is not None
        ):
            raise ModelPackOperationError(
                f"Runtime dependency is not exactly pinned with ==: {requirement}"
            )
        hashes = {
            token.removeprefix("--hash=sha256:")
            for token in tokens
            if token.startswith("--hash=sha256:")
        }
        if not hashes or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
            raise ModelPackOperationError(
                f"Runtime dependency lacks a valid SHA-256 hash: {requirement}"
            )
        unexpected = [token for token in tokens[1:] if not token.startswith("--hash=sha256:")]
        if unexpected:
            raise ModelPackOperationError(
                f"Runtime lock contains unsupported requirement options: {requirement}"
            )
        key = (canonicalize_name(parsed.name), versions[0])
        if key in locked:
            raise ModelPackOperationError(
                f"Runtime lock contains a duplicate requirement: {parsed.name}=={versions[0]}"
            )
        locked[key] = hashes

    wheels: dict[str, str] = {}
    matched: set[tuple[str, str]] = set()
    for path in sorted(wheelhouse.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix != ".whl":
            raise ModelPackOperationError(
                f"Runtime wheelhouse contains a non-wheel artifact: {path.name}"
            )
        try:
            distribution, version, _build, _tags = parse_wheel_filename(path.name)
        except ValueError as error:
            raise ModelPackOperationError(
                f"Runtime wheelhouse contains an invalid wheel filename: {path.name}"
            ) from error
        digest = _sha256_file(path)
        key = (canonicalize_name(distribution), str(version))
        expected = locked.get(key)
        if expected is None:
            raise ModelPackOperationError(
                f"Wheel {path.name} has no matching exact requirement in the runtime lock."
            )
        if digest not in expected:
            raise ModelPackOperationError(
                f"Wheel {path.name} SHA-256 is not attached to its matching requirement."
            )
        wheels[path.name] = digest
        matched.add(key)
    if not wheels:
        raise ModelPackOperationError("Runtime wheelhouse contains no wheel files.")
    absent = sorted(set(locked) - matched)
    if absent:
        raise ModelPackOperationError(
            "The offline wheelhouse is incomplete for the requirements lock; missing exact "
            "requirements: " + ", ".join(f"{name}=={version}" for name, version in absent)
        )
    return wheels


def lock_runtime_wheelhouse(wheelhouse: Path) -> bytes:
    """Create a deterministic one-wheel-per-package exact hash lock.

    Runtime releases are target-specific, so a reviewed wheelhouse must contain
    exactly one artifact for each normalized distribution.  Rejecting ambiguous
    duplicates keeps the generated lock and subsequent offline install auditable.
    """

    requirements: dict[str, tuple[str, str]] = {}
    for path in sorted(wheelhouse.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix != ".whl":
            raise ModelPackOperationError(
                f"Runtime wheelhouse contains a non-wheel artifact: {path.name}"
            )
        try:
            distribution, version, _build, _tags = parse_wheel_filename(path.name)
        except ValueError as error:
            raise ModelPackOperationError(
                f"Runtime wheelhouse contains an invalid wheel filename: {path.name}"
            ) from error
        name = canonicalize_name(distribution)
        if name in requirements:
            raise ModelPackOperationError(
                f"Runtime wheelhouse contains multiple wheels for distribution {name}."
            )
        requirements[name] = (str(version), _sha256_file(path))
    if not requirements:
        raise ModelPackOperationError("Runtime wheelhouse contains no wheel files.")
    return "".join(
        f"{name}=={version} --hash=sha256:{digest}\n"
        for name, (version, digest) in sorted(requirements.items())
    ).encode("utf-8")


def _git(source: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ModelPackOperationError(f"Could not snapshot pinned runtime source: {detail}")
    return completed.stdout


def _source_files(
    source: Path, recipe: RuntimeBuildRecipe
) -> tuple[dict[PurePosixPath, bytes], dict[str, str]]:
    entries = _git(source, "ls-tree", "-r", recipe.upstream_revision).decode("utf-8").splitlines()
    files: dict[PurePosixPath, bytes] = {}
    submodules: dict[str, str] = {}
    declared_submodules = {
        path.as_posix(): value for path, value in recipe.excluded_submodules.items()
    }
    for entry in entries:
        metadata, name = entry.split("\t", 1)
        mode, object_type, object_id = metadata.split()
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ModelPackOperationError(f"Unsafe Git source path: {name}")
        if mode == "160000" and object_type == "commit":
            if declared_submodules.get(name) != object_id:
                raise ModelPackOperationError(
                    f"Pinned source contains undeclared submodule {name} at {object_id}."
                )
            submodules[name] = object_id
            continue
        if object_type != "blob":
            raise ModelPackOperationError(f"Unsupported Git object in runtime source: {entry}")
        files[relative] = _git(source, "show", f"{recipe.upstream_revision}:{name}")
    if submodules != declared_submodules:
        raise ModelPackOperationError("Declared excluded submodules do not match the pinned tree.")
    return files, submodules


def _tree_digest(files: dict[PurePosixPath, bytes]) -> str:
    digest = hashlib.sha256()
    for path, payload in sorted(files.items(), key=lambda item: item[0].as_posix()):
        digest.update(path.as_posix().encode() + b"\0" + payload)
    return digest.hexdigest()


def _tar_entry(output: tarfile.TarFile, name: str, payload: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    output.addfile(info, io.BytesIO(payload))


def build_runtime_bundle(
    recipe: RuntimeBuildRecipe,
    *,
    source: Path,
    requirements_lock: Path,
    wheelhouse: Path,
    output_directory: Path,
    target: str,
) -> RuntimeBundleBuildResult:
    """Create deterministic source+wheelhouse bytes for a clean offline install."""

    if target not in recipe.supported_targets:
        raise ModelPackOperationError(f"Recipe {recipe.runtime_id} does not support {target}.")
    head = _git(source, "rev-parse", "HEAD").decode("ascii").strip()
    if head != recipe.upstream_revision:
        raise ModelPackOperationError(
            f"Source checkout is {head}; expected {recipe.upstream_revision}."
        )
    files, submodules = _source_files(source, recipe)
    missing_markers = [
        str(path) for path in recipe.source_markers if PurePosixPath(path.as_posix()) not in files
    ]
    if missing_markers:
        raise ModelPackOperationError(
            f"Pinned source revision lacks required markers: {', '.join(missing_markers)}"
        )
    lock_payload = requirements_lock.read_bytes()
    wheels = validate_hashed_requirements(lock_payload, wheelhouse)
    record = RuntimeBundleBuildRecord(
        runtime_id=recipe.runtime_id,
        revision=recipe.revision,
        upstream_repository=recipe.upstream_repository,
        upstream_revision=recipe.upstream_revision,
        target=target,
        python_version=recipe.python_version,
        source_tree_sha256=_tree_digest(files),
        requirements_sha256=_sha256_bytes(lock_payload),
        wheelhouse=wheels,
        import_modules=recipe.import_modules,
        excluded_submodules=submodules,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    filename = f"{recipe.runtime_id}-{recipe.revision[:12]}-{target}.tar.gz"
    destination = output_directory / filename
    with (
        destination.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w") as output,
    ):
        for path, payload in sorted(files.items(), key=lambda item: item[0].as_posix()):
            mode = 0o755 if path.parts and path.parts[0] in {"bin", "scripts"} else 0o644
            _tar_entry(output, path.as_posix(), payload, mode)
        _tar_entry(output, "_whoopy_bundle/requirements.lock", lock_payload)
        for wheel_name, _digest in sorted(wheels.items()):
            _tar_entry(
                output,
                f"_whoopy_bundle/wheelhouse/{wheel_name}",
                (wheelhouse / wheel_name).read_bytes(),
            )
        _tar_entry(
            output,
            "_whoopy_bundle/build.json",
            (record.model_dump_json(indent=2) + "\n").encode(),
        )
    return RuntimeBundleBuildResult(
        archive=destination,
        filename=filename,
        size_bytes=destination.stat().st_size,
        sha256=_sha256_file(destination),
        target=target,
        record=record,
    )


def bundle_manifest_snippet(result: RuntimeBundleBuildResult) -> str:
    """Return the exact reviewed registry entry; never edit hashes by hand."""

    return yaml.safe_dump(
        {
            "target": result.target,
            "filename": result.filename,
            "size_bytes": result.size_bytes,
            "sha256": result.sha256,
            "archive": "tar.gz",
            "install_mode": result.install_mode,
            "url": None,
        },
        sort_keys=False,
    )


def validate_runtime_releases(
    runtime_manifest: Path, build_recipes: Path, repository_root: Path
) -> list[str]:
    """Fail closed when a published runtime lacks reproducible build provenance."""

    # Local import avoids coupling the builder's schema declarations back into
    # the installer's module initialization.
    from whoopy.model_packs.runtime import load_runtime_pack_manifest

    packs = load_runtime_pack_manifest(runtime_manifest)
    recipes = load_runtime_build_recipes(build_recipes)
    by_id = {item.runtime_id: item for item in recipes.runtimes}
    messages: list[str] = []
    for pack in packs.runtimes:
        recipe = by_id.get(pack.runtime_id)
        if pack.bundles and recipe is None:
            raise ModelPackOperationError(
                f"Published runtime {pack.runtime_id} has no reviewed build recipe."
            )
        if recipe is None:
            messages.append(
                f"{pack.runtime_id}: blocked — no reviewed source revision or build recipe"
            )
            continue
        if (
            recipe.revision != pack.revision
            or recipe.upstream_repository != pack.upstream_repository
            or recipe.upstream_revision != pack.upstream_revision
            or recipe.python_version != pack.python_version
        ):
            raise ModelPackOperationError(
                f"Runtime {pack.runtime_id} build recipe disagrees with runtime registry."
            )
        for target, declared_lock in recipe.lock_paths.items():
            if not (repository_root / declared_lock).is_file():
                raise ModelPackOperationError(
                    f"Reviewed {pack.runtime_id}/{target} lock is missing: {declared_lock}"
                )
        for bundle in pack.bundles:
            bundle_lock = recipe.lock_paths.get(bundle.target)
            if bundle_lock is None:
                raise ModelPackOperationError(
                    f"Published {pack.runtime_id}/{bundle.target} has no reviewed lock path."
                )
            if bundle.install_mode != "locked-wheelhouse":
                raise ModelPackOperationError(
                    f"New published runtime {pack.runtime_id}/{bundle.target} must use the clean "
                    "locked-wheelhouse install mode."
                )
            expected_filename = f"{recipe.runtime_id}-{recipe.revision[:12]}-{bundle.target}.tar.gz"
            if bundle.archive != "tar.gz" or bundle.filename != expected_filename:
                raise ModelPackOperationError(
                    f"Published {pack.runtime_id}/{bundle.target} does not use the deterministic "
                    f"builder filename {expected_filename}."
                )
        missing = sorted(set(recipe.supported_targets) - set(recipe.lock_paths))
        published = {bundle.target for bundle in pack.bundles}
        unpublished = sorted(set(recipe.supported_targets) - published)
        if not pack.bundles:
            state = "blocked"
        elif unpublished:
            state = "partially published"
        else:
            state = "published"
        if missing:
            detail = f"missing reviewed locks for {', '.join(missing)}"
        elif unpublished:
            detail = (
                "reviewed locks exist; no checksum-reviewed bundle is published for "
                + ", ".join(unpublished)
            )
        else:
            detail = "all declared targets have reviewed locks and published bundles"
        messages.append(f"{pack.runtime_id}: {state} — {detail}")
    return messages
