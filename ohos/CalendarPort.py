"""Calendar 2.10.1 独立 OHOS producer；从固定 Git blob 制备隔离编译源。"""
from __future__ import annotations

import argparse
from collections import Counter
import time
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
from urllib.request import ProxyHandler, build_opener
import xml.etree.ElementTree as ET
import zipfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
TOOL_REVISION = "d0cff88fd1ea2e4f08bb4409f1c802cca0c7f285"
TOOL_DIR = "toolchains/kotlin-native/2.4.20-0.1.0-08"
GROUP = "io.github.qsxs.cpf.calendar"
MODULE = "compose-multiplatform"
ROOT = HERE.parent
physical = Path
TASKS = {"Tasks": ["tasks", "--all"], "Resolve": ["resolveCalendar"], "Metadata": ["recordCalendarMetadata"],
         "NativeX64": ["recordCalendarX64"], "NativeArm64": ["recordCalendarArm64"], "Publish": ["publishCalendar"]}
COMPILES = (("Metadata", "metadata", None), ("NativeX64", "native-x64", "ohos_x64"),
            ("NativeArm64", "native-arm64", "ohos_arm64"))
REQUIRED = {"resolveCalendar", "compileCommonMainKotlinMetadata", "compileKotlinOhosX64",
            "compileKotlinOhosArm64", "allMetadataJar", "cinteropCalendarIcuOhosX64", "cinteropCalendarIcuOhosArm64",
            "recordCalendarMetadata", "recordCalendarX64", "recordCalendarArm64", "publishCalendar"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def record(path):
    path = Path(path)
    data = path.read_bytes()
    return {"path": str(path.resolve()), "size": len(data), "sha256": digest(data)}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as output:
            output.write(text)
    else:
        path.write_text(text, encoding="utf-8")


def safe_environment(work=None):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "USERPROFILE", "SystemRoot", "ComSpec", "JAVA_HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "LD_LIBRARY_PATH") if key in os.environ}
    if work is not None:
        for key, folder in (("GRADLE_USER_HOME", "gradle-home"), ("KONAN_DATA_DIR", "konan"),
                            ("TMPDIR", "temp"), ("TEMP", "temp"), ("TMP", "temp")):
            path = Path(work) / folder
            path.mkdir(parents=True, exist_ok=True)
            env[key] = str(path)
    return env


def checked_tools(work, explicit):
    lock = load(HERE / "tooling-lock.json")
    if lock["revision"] != TOOL_REVISION or lock["repository"] != "https://github.com/qsxs/cpf-port-tooling":
        raise RuntimeError("Immutable shared tooling revision differs")
    if lock["archiveUrl"] != lock["repository"] + "/archive/" + TOOL_REVISION + ".tar.gz":
        raise RuntimeError("Shared tooling archive URL differs")
    if len(lock["files"]) != 20 or len({row["path"] for row in lock["files"]}) != 20:
        raise RuntimeError("Shared tooling file lock differs")
    directory = Path(explicit).resolve() if explicit else (work / "tooling" / TOOL_REVISION).resolve()
    for row in lock["files"]:
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in row["path"] or not row["path"].startswith(TOOL_DIR + "/"):
            raise RuntimeError("Unsafe shared tooling file path")
    if not explicit and not all((directory / row["path"]).is_file() for row in lock["files"]):
        # 仅读取锁定文件；归档压缩容器本身不作为内容身份。
        with build_opener(ProxyHandler({})).open(lock["archiveUrl"], timeout=60) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise RuntimeError("Shared tooling archive is too large")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            for row in lock["files"]:
                matches = [member for member in members if "/" in member.name and member.name.split("/", 1)[1] == row["path"]]
                if len(matches) != 1 or not matches[0].isfile():
                    raise RuntimeError("Shared tooling archive member differs: " + row["path"])
                stream = archive.extractfile(matches[0])
                if stream is None:
                    raise RuntimeError("Shared tooling archive member is unreadable")
                content = stream.read(row["size"] + 1)
                if len(content) != row["size"] or digest(content) != row["sha256"]:
                    raise RuntimeError("Shared tooling archive bytes differ: " + row["path"])
                target = directory / row["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.read_bytes() != content:
                        raise RuntimeError("Existing shared tooling bytes differ")
                else:
                    with target.open("xb") as out:
                        out.write(content)
    for row in lock["files"]:
        actual = record(directory / row["path"])
        if (actual["size"], actual["sha256"]) != (row["size"], row["sha256"]):
            raise RuntimeError("Shared tooling identity differs: " + row["path"])
    return directory


def git_bytes(source_sha, path):
    env = safe_environment()
    env["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.check_output(["git", "-C", str(ROOT), "show", source_sha + ":" + path], env=env)


def platform_destination(relative):
    prefix = "compose-multiplatform/library/src/ohosMain/kotlin/"
    definition = "compose-multiplatform/library/src/nativeInterop/cinterop/calendar_icu.def"
    if relative.startswith(prefix) and relative.endswith(".kt"):
        return "ohosMain/" + relative.removeprefix(prefix)
    if relative == definition:
        return "nativeInterop/cinterop/calendar_icu.def"
    raise RuntimeError("Unexpected Calendar platform input: " + relative)


def source_facts(source_sha, work=None, *, allow_uncommitted=False):
    model = load(HERE / "source-inputs.json")
    cfg = load(HERE / "port-config.json")
    platform = load(HERE / "platform-inputs.json")
    for original in load(HERE / "legal/origins.json"):
        actual = record(HERE / original["path"])
        if (actual["size"], actual["sha256"]) != (original["size"], original["sha256"]):
            raise RuntimeError("Calendar upstream provenance differs: " + original["path"])
    for key in ("sources", "gmm", "pom"):
        original = model[key]
        actual = record(HERE / original["path"])
        if (actual["size"], actual["sha256"]) != (original["size"], original["sha256"]):
            raise RuntimeError("Official Calendar input differs: " + key)
    wrapper = {"gradle/wrapper/gradle-wrapper.jar": "7d3a4ac4de1c32b59bc6a4eb8ecb8e612ccd0cf1ae1e99f66902da64df296172",
               "gradle/wrapper/gradle-wrapper.properties": "93b4f2c5ddcfc9e6f630fc7e004b31b389695cd0228be6da42e196e5d4af7acb"}
    for path, expected in wrapper.items():
        if record(HERE / path)["sha256"] != expected:
            raise RuntimeError("Approved Gradle wrapper differs: " + path)
    for key in ("sourcePom", "serializationSourcePom"):
        item = load(HERE / "compiler-plugin-toolchain.json")[key]
        actual = record(HERE / item["path"])
        if (actual["size"], actual["sha256"]) != (item["size"], item["sha256"]):
            raise RuntimeError("Calendar compiler plugin POM provenance differs: " + key)
    if digest(git_bytes(source_sha, "LICENSE.md")) != cfg["modules"][MODULE]["licenseSha256"]:
        raise RuntimeError("Calendar upstream MIT license blob differs")
    common = "compose-multiplatform/library/src/commonMain/kotlin/"
    ohos = "compose-multiplatform/library/src/ohosMain/kotlin/"
    resources = "compose-multiplatform/library/src/commonMain/composeResources/"
    roots = [common.rstrip("/"), ohos.rstrip("/"), resources.rstrip("/"), "compose-multiplatform/library/src/nativeInterop/cinterop"]
    env = safe_environment()
    env["GIT_NO_LAZY_FETCH"] = "1"
    tree = subprocess.check_output(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", source_sha, "--", *roots], env=env)
    blobs = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        info, path = entry.decode("utf-8").split("\t", 1)
        mode, kind, oid = info.split(" ")
        if kind != "blob" or mode != "100644":
            raise RuntimeError("Unexpected Calendar Git tree entry: " + path)
        blobs[path] = oid
    if any(path.startswith(resources) for path in blobs):
        raise RuntimeError("Calendar upstream composeResources asset inventory changed")
    members = model["members"]
    if len(members) != 51 or len({item["member"] for item in members}) != 51:
        raise RuntimeError("Calendar official source archive inventory differs")
    expected_common = {common + item["member"].removeprefix("commonMain/") for item in members if item["compile"]}
    actual_common = {path for path in blobs if path.startswith(common) and path.endswith(".kt")}
    if len(expected_common) != 41 or actual_common != expected_common:
        raise RuntimeError("Calendar complete handwritten common Git source set differs")
    rows = []
    with zipfile.ZipFile(HERE / model["sources"]["path"]) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or {name for name in names if not name.endswith("/")} != {item["member"] for item in members}:
            raise RuntimeError("Calendar source archive members differ")
        for item in members:
            data = archive.read(item["member"])
            if len(data) != item["size"] or digest(data) != item["sha256"]:
                raise RuntimeError("Calendar official source archive bytes differ: " + item["member"])
            row = dict(item)
            if item["compile"]:
                git_path = common + item["member"].removeprefix("commonMain/")
                if git_bytes(source_sha, git_path) != data:
                    raise RuntimeError("Calendar common Git blob differs from source JAR: " + item["member"])
                if work is not None:
                    actual = record(Path(work) / "sources" / Path(*PurePosixPath(item["member"]).parts))
                    if (actual["size"], actual["sha256"]) != (item["size"], item["sha256"]):
                        raise RuntimeError("Calendar isolated source bytes differ: " + item["member"])
                    row.update(actual)
            rows.append(row)
    selected = platform["files"]
    actual_ohos = {path for path in blobs if path.startswith(ohos) and path.endswith(".kt")}
    expected_ohos = {item["path"] for item in selected if item["path"].endswith(".kt")}
    if actual_ohos and actual_ohos != expected_ohos:
        raise RuntimeError("Calendar committed OHOS actual inventory differs")
    platforms = []
    for item in selected:
        relative = item["path"]
        if relative in blobs:
            data = git_bytes(source_sha, relative)
            origin = "COMMITTED_GIT_BLOB"
        elif allow_uncommitted:
            data = (ROOT / relative).read_bytes()
            origin = "UNCOMMITTED_WORKTREE_ONLY"
        else:
            raise RuntimeError("Calendar platform input is not committed in source SHA: " + relative)
        if (len(data), digest(data)) != (item["size"], item["sha256"]):
            raise RuntimeError("Calendar platform input bytes differ: " + relative)
        row = {"relativePath": relative, "size": len(data), "sha256": digest(data), "origin": origin}
        if work is not None:
            actual = record(Path(work) / "sources" / Path(*PurePosixPath(platform_destination(relative)).parts))
            if (actual["size"], actual["sha256"]) != (item["size"], item["sha256"]):
                raise RuntimeError("Calendar isolated platform bytes differ: " + relative)
            row.update(actual)
        platforms.append(row)
    actuals = [row for row in platforms if row["relativePath"].endswith(".kt")]
    defs = [row for row in platforms if row["relativePath"].endswith(".def")]
    if len(actuals) != 2 or len(defs) != 1:
        raise RuntimeError("Calendar actual or ICU binding inventory differs")
    return model, rows, actuals, defs[0]


def sysroot_facts(path, kind, sdk_package):
    root = Path(path).resolve()
    candidate = load(HERE / "sysroot-input.json")["candidate"]
    selected = candidate["files"]
    files = []
    for item in selected:
        actual = record(root / item["path"])
        if kind == "cpf" and (actual["size"], actual["sha256"]) != (item["size"], item["sha256"]):
            raise RuntimeError("Pinned CPF sysroot input differs: " + item["path"])
        files.append({"relativePath": item["path"], **actual})
    package = record(sdk_package) if sdk_package else None
    return {"kind": kind, "files": files, "sdkPackage": package,
            "candidateArchive": {k: candidate[k] for k in ("url", "size", "sha256")} if kind == "cpf" else None,
            "candidateArchiveVerification": "LOCK_ONLY_NOT_READ_BY_PORT" if kind == "cpf" else None,
            "devecoVersion": "CALLER_SELECTED_26.0.0.105_NOT_INDEPENDENTLY_VERIFIED" if kind == "deveco" else None,
            "libraryBundled": False, "abiOrRuntimeEquivalence": "NOT_ESTABLISHED"}


def output_roots(args):
    root = ROOT.resolve()
    work, repository = Path(args.work).resolve(), Path(args.repository).resolve()
    if work.is_relative_to(repository) or repository.is_relative_to(work):
        raise RuntimeError("Work and staging must be separate directories")
    if any(root.is_relative_to(path) or path.is_relative_to(root) for path in (work, repository)):
        raise RuntimeError("Work and staging must be outside and not contain the checkout")
    if args.sysroot:
        sysroot = Path(args.sysroot).resolve()
        if any(sysroot.is_relative_to(path) or path.is_relative_to(sysroot) for path in (work, repository)):
            raise RuntimeError("Sysroot must be separate from work and staging")
    return work, repository


def recipe_inputs():
    result = []
    for current, directories, files in os.walk(HERE):
        parent = Path(current)
        directories[:] = [name for name in directories if name not in (".local", ".gradle", ".kotlin", "build", "__pycache__")]
        for name in files:
            path = parent / name
            if path.is_file():
                data = path.read_bytes()
                result.append({"path": path.relative_to(HERE).as_posix(), "size": len(data), "sha256": digest(data)})
    return sorted(result, key=lambda item: item["path"])


def verify_recipe_commit(source_sha, recipe):
    env = safe_environment()
    env["GIT_NO_LAZY_FETCH"] = "1"
    tree = subprocess.check_output(["git", "-C", str(ROOT), "ls-tree", "-r", "-z", source_sha, "--", "ohos"], env=env)
    committed = {}
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        info, path = entry.decode("utf-8").split("\t", 1)
        mode, kind, oid = info.split(" ")
        relative = path.removeprefix("ohos/")
        if any(part in (".local", ".gradle", ".kotlin", "build", "__pycache__") for part in PurePosixPath(relative).parts[:-1]):
            continue
        if kind != "blob" or mode not in ("100644", "100755"):
            raise RuntimeError("Unsupported committed Calendar recipe entry: " + path)
        committed[relative] = oid
    if set(committed) != {item["path"] for item in recipe}:
        raise RuntimeError("Calendar recipe files differ from requested committed source SHA")
    for item in recipe:
        data = (HERE / item["path"]).read_bytes()
        header = b"blob " + str(len(data)).encode("ascii") + b"\0"
        if hashlib.sha1(header + data).hexdigest() != committed[item["path"]]:
            raise RuntimeError("Calendar recipe bytes differ from committed source SHA: " + item["path"])


def identity(args, tools, outputs):
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha) or not re.fullmatch(r"[1-9][0-9]*", args.run_id) or not re.fullmatch(r"[1-9][0-9]*", args.run_attempt):
        raise RuntimeError("Full source SHA and positive run identifiers are required")
    actual = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], env=safe_environment(), check=True, capture_output=True, text=True).stdout.strip()
    if actual != args.source_sha:
        raise RuntimeError("Calendar checkout differs from requested source SHA")
    cfg = load(HERE / "port-config.json")
    if cfg["modules"][MODULE]["group"] != GROUP or cfg["kotlin"] != "2.4.20-0.1.0-08" or cfg["metadataPolicy"] != "STANDARD_KGP_FIRST_NO_PREEMPTIVE_FALLBACK":
        raise RuntimeError("Calendar port identity differs")
    model, rows, actuals, definition = source_facts(args.source_sha)
    sdk = sysroot_facts(args.sysroot, args.sysroot_kind, args.sdk_package)
    recipe = recipe_inputs()
    source_rows = [{key: row[key] for key in ("member", "size", "sha256", "compile")} for row in rows]
    platform_rows = [{key: row[key] for key in ("relativePath", "size", "sha256")} for row in [*actuals, definition]]
    sdk_identity = {"kind": sdk["kind"], "files": [{key: row[key] for key in ("relativePath", "size", "sha256")} for row in sdk["files"]],
                    "sdkPackage": {key: sdk["sdkPackage"][key] for key in ("size", "sha256")} if sdk["sdkPackage"] else None,
                    "candidateArchive": sdk["candidateArchive"], "candidateArchiveVerification": sdk["candidateArchiveVerification"],
                    "devecoVersion": sdk["devecoVersion"]}
    version = "2.10.1-cpf11-preview-" + args.source_sha[:12] + "-" + args.run_id + "-" + args.run_attempt
    fingerprint_value = {"sourceCommit": args.source_sha, "group": GROUP, "module": MODULE, "version": version,
                         "toolingCommit": TOOL_REVISION, "recipe": recipe, "members": source_rows, "platform": platform_rows, "sysroot": sdk_identity}
    fingerprint = digest(json.dumps(fingerprint_value, sort_keys=True, separators=(",", ":")).encode())
    return {**fingerprint_value, "fingerprint": fingerprint, "sysrootPath": str(Path(args.sysroot).resolve()), "sysrootEvidence": sdk,
            "work": str(outputs[0]), "repository": str(outputs[1]), "toolingHome": str(tools)}


def check_native(home):
    if not home:
        raise RuntimeError("Explicit Kotlin/Native home is required")
    props = dict(line.split("=", 1) for line in (Path(home) / "konan/konan.properties").read_text(encoding="utf-8").splitlines() if "=" in line)
    if props.get("compilerVersion", "").strip() != "2.4.20-0.1.0-08":
        raise RuntimeError("Kotlin/Native compilerVersion differs")


def verify_stage(value, stage, target):
    work = Path(value["work"])
    row = load(work / "evidence" / (stage + ".json"))
    model, members, actuals, _ = source_facts(value["sourceCommit"], work=work)
    if (row["fingerprint"] != value["fingerprint"] or row["stage"] != stage or row["sysroot"] != value["sysrootPath"] or
            row["sourceManifest"] != record(work / "source-manifest.json") or
            row["handwrittenCommonCount"] != 41 or row["actualCount"] != (0 if target is None else 2)):
        raise RuntimeError("Calendar compile identity differs: " + stage)
    expected = {r["path"]: (r["size"], r["sha256"]) for r in members if r["compile"]}
    if target is not None:
        expected.update({r["path"]: (r["size"], r["sha256"]) for r in actuals})
    sources = {r["path"]: (r["size"], r["sha256"]) for r in row["sources"]}
    generated = row["generatedResources"]
    if len(sources) != len(row["sources"]) or any(sources.get(path) != info for path, info in expected.items()):
        raise RuntimeError("Calendar original source closure differs")
    if set(sources) - set(expected) != {r["path"] for r in generated} or not generated:
        raise RuntimeError("Calendar generated resource source set differs")
    generated_root = (work / "build/generated/compose").resolve()
    for item in row["sources"]:
        if record(item["path"]) != item:
            raise RuntimeError("Calendar source bytes changed")
    for item in generated:
        if not Path(item["path"]).resolve().is_relative_to(generated_root):
            raise RuntimeError("Calendar resource source escaped formal generated directory")
    names = {Path(item["path"]).name for item in generated}
    if not {"Res.kt", "ExpectResourceCollectors.kt"}.issubset(names) or (target is not None and "ActualResourceCollectors.kt" not in names):
        raise RuntimeError("Calendar formal resource generators are incomplete")
    generated_text = "\n".join(Path(item["path"]).read_text(encoding="utf-8") for item in generated)
    if not all(name in generated_text for name in ("allDrawableResources", "allStringResources", "allStringArrayResources", "allPluralStringResources", "allFontResources")):
        raise RuntimeError("Calendar formal resource collectors differ")
    outputs = [item for producer in row["resourceProducers"] for item in producer["outputs"]]
    if not row["resourceProducers"] or not {r["path"] for r in generated}.issubset({r["path"] for r in outputs}):
        raise RuntimeError("Calendar generated sources lack producer outputs")
    for item in outputs:
        if record(item["path"]) != item:
            raise RuntimeError("Calendar resource producer output changed")
    plugins = row["compilerPlugins"]
    tool = load(HERE / "compiler-plugin-toolchain.json")
    ids = ("org.jetbrains.kotlin:kotlin-compose-compiler-plugin-embeddable", "org.jetbrains.kotlin:kotlin-serialization-compiler-plugin-embeddable")
    expected_plugins = {(tool["artifacts"][key]["size"], tool["artifacts"][key]["sha256"]) for key in ids}
    if (len(plugins) != 2 or len(expected_plugins) != 2 or {(r["size"], r["sha256"]) for r in plugins} != expected_plugins or
            any(tool["pins"][key] != "2.4.20-0.1.0-08" for key in ids) or any(record(r["path"]) != r for r in plugins)):
        raise RuntimeError("Calendar compiler plugin set differs")
    expected_scopes = {
        "commonImplementation": {"org.jetbrains.compose.runtime:runtime", "org.jetbrains.compose.foundation:foundation",
            "org.jetbrains.compose.ui:ui", "org.jetbrains.compose.components:components-resources",
            "org.jetbrains.compose.components:components-ui-tooling-preview", "org.jetbrains.compose.annotation-internal:annotation",
            "org.jetbrains.kotlinx:kotlinx-serialization-core"},
        "commonApi": {"org.jetbrains.kotlinx:kotlinx-datetime"},
        "ohosApi": {"org.jetbrains.kotlinx:kotlinx-serialization-core"},
    }
    if set(row["sourceDependencies"]) != set(expected_scopes) or any(
            set(row["sourceDependencies"][scope]) != expected for scope, expected in expected_scopes.items()):
        raise RuntimeError("Calendar source dependency scopes differ")
    if target is None:
        if row["interopDefinition"] is not None or row["interopHeaders"]:
            raise RuntimeError("Calendar metadata unexpectedly records Native cinterop inputs")
    else:
        definition = work / "sources/nativeInterop/cinterop/calendar_icu.def"
        if row["interopDefinition"] != record(definition):
            raise RuntimeError("Calendar cinterop definition differs")
        headers = [{key: item[key] for key in ("path", "size", "sha256")} for item in value["sysrootEvidence"]["files"]
                   if item["relativePath"].startswith("usr/include/")]
        if sorted(row["interopHeaders"], key=lambda item: item["path"]) != sorted(headers, key=lambda item: item["path"]):
            raise RuntimeError("Calendar selected cinterop header inputs differ")
    tasks = {r["task"] for r in row["producers"]}
    expected_tasks = ({":compileCommonMainKotlinMetadata", ":allMetadataJar"} if target is None else
        ({":compileKotlinOhosX64", ":cinteropCalendarIcuOhosX64"} if target == "ohos_x64" else {":compileKotlinOhosArm64", ":cinteropCalendarIcuOhosArm64"}))
    if tasks != expected_tasks:
        raise RuntimeError("Calendar producer task set differs")
    for producer in row["producers"]:
        if not producer["outputs"] or not any(item["size"] > 0 for item in producer["outputs"]):
            raise RuntimeError("Calendar producer outputs are empty")
        for item in producer["outputs"]:
            if record(item["path"]) != item:
                raise RuntimeError("Calendar compiler output bytes changed")
    compiler_task = {None: ":compileCommonMainKotlinMetadata", "ohos_x64": ":compileKotlinOhosX64", "ohos_arm64": ":compileKotlinOhosArm64"}[target]
    compiler_outputs = next(item["outputs"] for item in row["producers"] if item["task"] == compiler_task)
    has_metadata, has_ir, native_targets = False, False, []
    for item in compiler_outputs:
        path = Path(item["path"])
        props = None
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                has_metadata |= any(("/linkdata/" in name or name.endswith(".kotlin_metadata")) and archive.getinfo(name).file_size > 0 for name in names)
                has_ir |= any("/ir/" in name and archive.getinfo(name).file_size > 0 for name in names)
                if "default/manifest" in names:
                    props = archive.read("default/manifest").decode()
        elif path.as_posix().endswith("/default/manifest"):
            props = path.read_text(encoding="utf-8")
        if "/linkdata/" in path.as_posix() and item["size"] > 0:
            has_metadata = True
        if "/ir/" in path.as_posix() and item["size"] > 0:
            has_ir = True
        if props is not None:
            properties = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
            native_targets.extend(properties.get("native_targets", "").split())
    if not has_metadata or (target is not None and (not has_ir or set(native_targets) != {target})):
        raise RuntimeError("Calendar compiler metadata, Native target or IR differs: " + stage)
    return row


def verify_native_interop(value, stage, *, create=False):
    target = "ohos_x64" if stage == "NativeX64" else "ohos_arm64"
    evidence_name = "native-x64" if stage == "NativeX64" else "native-arm64"
    evidence = load(Path(value["work"]) / "evidence" / (evidence_name + ".json"))
    if evidence["fingerprint"] != value["fingerprint"]:
        raise RuntimeError("Calendar Native stage identity differs")
    manifests = []
    for producer in evidence["producers"]:
        outputs = producer["outputs"]
        for item in outputs:
            path = physical(item["path"])
            if record(item["path"]) != item:
                raise RuntimeError("Calendar Native stage output bytes changed")
            if path.suffix == ".klib":
                with zipfile.ZipFile(path) as archive:
                    text = archive.read("default/manifest").decode()
            elif path.as_posix().endswith("/default/manifest"):
                text = path.read_text(encoding="utf-8")
            else:
                continue
            props = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
            if set(props.get("native_targets", "").split()) != {target}:
                raise RuntimeError("Calendar Native Klib target differs")
            if "cinteropCalendarIcu" in producer["task"] and "-licu" not in props.get("linkerOpts", "").split():
                raise RuntimeError("Calendar ICU CInterop must retain system -licu")
            manifests.append({"task": producer["task"], "file": item, "properties": props})
    if not any("cinteropCalendarIcu" in row["task"] for row in manifests):
        raise RuntimeError("Calendar ICU CInterop Klib manifest absent")
    result = {"fingerprint": value["fingerprint"], "target": target, "manifests": manifests, "systemLibrary": "-licu",
              "sysrootInputs": value["sysrootEvidence"], "runtime": "NOT_RUN"}
    path = Path(value["work"]) / "evidence" / (evidence_name + "-interop.json")
    if create:
        write(path, result, exclusive=True)
    elif load(path) != result:
        raise RuntimeError("Calendar interop evidence differs")
    return result


API_SYMBOLS = (
    "com.kizitonwose.calendar.compose/HorizontalCalendar",
    "com.kizitonwose.calendar.compose/VerticalCalendar",
    "com.kizitonwose.calendar.compose/WeekCalendar",
    "com.kizitonwose.calendar.compose/HeatMapCalendar",
    "com.kizitonwose.calendar.compose/HorizontalYearCalendar",
    "com.kizitonwose.calendar.compose/VerticalYearCalendar",
    "com.kizitonwose.calendar.compose/rememberCalendarState",
    "com.kizitonwose.calendar.compose/CalendarState",
    "com.kizitonwose.calendar.compose.weekcalendar/rememberWeekCalendarState",
    "com.kizitonwose.calendar.compose.weekcalendar/WeekCalendarState",
    "com.kizitonwose.calendar.compose.heatmapcalendar/rememberHeatMapCalendarState",
    "com.kizitonwose.calendar.compose.heatmapcalendar/HeatMapCalendarState",
    "com.kizitonwose.calendar.compose.yearcalendar/rememberYearCalendarState",
    "com.kizitonwose.calendar.compose.yearcalendar/YearCalendarState",
    "com.kizitonwose.calendar.core/CalendarDay",
    "com.kizitonwose.calendar.core/CalendarMonth",
    "com.kizitonwose.calendar.core/CalendarYear",
)


def native_klib(outputs):
    manifests = [item for item in outputs if item["path"].replace("\\", "/").endswith("/default/manifest")]
    packed = [item for item in outputs if item["path"].endswith(".klib")]
    if len(manifests) == 1:
        return Path(manifests[0]["path"]).parent.parent
    if len(packed) == 1:
        return Path(packed[0]["path"])
    raise RuntimeError("Calendar Native compiler Klib root is not uniquely identified")


def verify_api(value, stage, target, native_home):
    row = verify_stage(value, stage, target)
    api = load(Path(value["work"]) / "evidence" / (stage + "-api.json"))
    task = ":compileKotlinOhosX64" if target == "ohos_x64" else ":compileKotlinOhosArm64"
    outputs = next(item["outputs"] for item in row["producers"] if item["task"] == task)
    if (api["fingerprint"] != value["fingerprint"] or api["stage"] != stage or api["target"] != target or
            api["compilerOutputs"] != outputs or api["symbols"] != list(API_SYMBOLS)):
        raise RuntimeError("Calendar API evidence does not belong to the compiler output")
    launcher = Path(native_home).resolve() / ("bin/klib.bat" if os.name == "nt" else "bin/klib")
    receipt, output = api["invocation"], api["output"]
    if record(receipt["path"]) != receipt or record(output["path"]) != output:
        raise RuntimeError("Calendar API invocation or output differs")
    invocation = load(receipt["path"])
    if (invocation["exitCode"] != 0 or invocation["fingerprint"] != value["fingerprint"] or invocation["inputs"] != outputs or
            invocation["argv"] != [str(launcher), "dump-metadata-signatures", str(native_klib(outputs))] or
            invocation["launcher"] != record(launcher) or invocation["output"] != output or
            Path(output["path"]).resolve() != Path(receipt["path"]).with_suffix(".stdout.log").resolve()):
        raise RuntimeError("Calendar API signature command identity differs")
    text = Path(output["path"]).read_text(encoding="utf-8-sig")
    missing = [name for name in API_SYMBOLS if not re.search(re.escape(name) + r"\|", text)]
    if missing:
        raise RuntimeError("Calendar representative API signatures absent: " + ", ".join(missing))
    return api


def signature_stage(value, stage, target, attempt, native_home):
    row = verify_stage(value, stage, target)
    task = ":compileKotlinOhosX64" if target == "ohos_x64" else ":compileKotlinOhosArm64"
    outputs = next(item["outputs"] for item in row["producers"] if item["task"] == task)
    launcher = Path(native_home).resolve() / ("bin/klib.bat" if os.name == "nt" else "bin/klib")
    argv = [str(launcher), "dump-metadata-signatures", str(native_klib(outputs))]
    work = Path(value["work"])
    receipt = work / "invocations" / (stage + "-signatures-" + str(attempt) + ".json")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    invocation = {"fingerprint": value["fingerprint"], "argv": argv, "cwd": str(ROOT), "launcher": record(launcher),
                  "inputs": outputs, "started": time.time(), "exitCode": None}
    write(receipt, invocation, exclusive=True)
    with receipt.with_suffix(".stdout.log").open("xb") as out, receipt.with_suffix(".stderr.log").open("xb") as err:
        result = subprocess.run(argv, cwd=ROOT, env=safe_environment(work), stdout=out, stderr=err)
    output = record(receipt.with_suffix(".stdout.log"))
    invocation.update(exitCode=result.returncode, finished=time.time(), output=output)
    write(receipt, invocation)
    if result.returncode:
        raise RuntimeError("Calendar Native API signature dump failed; see " + str(receipt))
    write(work / "evidence" / (stage + "-api.json"), {"fingerprint": value["fingerprint"], "stage": stage, "target": target,
          "compilerOutputs": outputs, "symbols": list(API_SYMBOLS), "invocation": record(receipt), "output": output,
          "coverage": "REPRESENTATIVE_SIGNATURES_FULL_SOURCE_COMPILED", "runtime": "NOT_RUN"}, exclusive=True)
    return verify_api(value, stage, target, native_home)


def verify_publication(value, native_home):
    stages = [verify_stage(value, name, target) for name, target in (("metadata", None), ("native-x64", "ohos_x64"), ("native-arm64", "ohos_arm64"))]
    interops = [verify_native_interop(value, label) for label in ("NativeX64", "NativeArm64")]
    api_receipts = [verify_api(value, name, target, native_home) for name, target in (("native-x64", "ohos_x64"), ("native-arm64", "ohos_arm64"))]
    if any(path.is_file() and path.name == "libicu.so" for path in Path(value["repository"]).rglob("*")):
        raise RuntimeError("System ICU library must not be staged")
    pins = load(HERE / "dependency-pins.json")
    metadata_pins = load(HERE / "metadata-pins.json")
    model = load(HERE / "source-inputs.json")
    repository = Path(value["repository"]) / GROUP.replace(".", "/")
    version = value["version"]
    root_file = repository / MODULE / version / (MODULE + "-" + version + ".module")
    root = load(root_file)
    files, scopes = [], []
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    upstream_pom = ET.fromstring((HERE / model["pom"]["path"]).read_bytes())
    upstream_developers = [tuple(item.findtext("m:" + key, namespaces=ns) for key in ("id", "name", "url"))
                           for item in upstream_pom.findall("m:developers/m:developer", ns)]
    for suffix, target in (("", None), ("-ohosx64", "ohos_x64"), ("-ohosarm64", "ohos_arm64")):
        name = MODULE + suffix
        directory = repository / name / version
        module_file = directory / (name + "-" + version + ".module")
        gmm = load(module_file)
        if any(gmm["component"].get(key) != expected for key, expected in (("group", GROUP), ("module", MODULE), ("version", version))):
            raise RuntimeError("Calendar GMM owner differs: " + name)
        if target:
            if (directory / gmm["component"]["url"]).resolve() != root_file.resolve():
                raise RuntimeError("Calendar Native leaf owner differs")
            links = [variant["available-at"] for variant in root["variants"] if variant.get("attributes", {}).get("org.jetbrains.kotlin.native.target") == target and "available-at" in variant]
            if not links or any(link["group"] != GROUP or link["module"] != name or link["version"] != version or (root_file.parent / link["url"]).resolve() != module_file.resolve() for link in links):
                raise RuntimeError("Calendar root Native link differs")
        api = []
        for variant in gmm["variants"]:
            attrs = variant.get("attributes", {})
            if (target is None and attrs.get("org.gradle.usage") == "kotlin-metadata" and attrs.get("org.jetbrains.kotlin.platform.type") == "common") or (target is not None and attrs.get("org.gradle.usage") == "kotlin-api" and attrs.get("org.jetbrains.kotlin.native.target") == target):
                api.append(variant)
            for dep in variant.get("dependencies", []):
                ga = dep["group"] + ":" + dep["module"]
                if pins.get(ga) != dep.get("version", {}).get("requires"):
                    raise RuntimeError("Calendar GMM dependency differs: " + ga)
            for item in variant.get("files", []):
                path = (directory / item["url"]).resolve()
                if not path.is_relative_to(directory.resolve()):
                    raise RuntimeError("Calendar publication artifact escaped version directory")
                actual = record(path)
                if actual["size"] != item["size"] or actual["sha256"] != item["sha256"] or actual["size"] <= 0:
                    raise RuntimeError("Calendar publication artifact differs")
        if not api or not all(variant.get("files") for variant in api):
            raise RuntimeError("Calendar public API artifact is absent")
        expected_api = {"org.jetbrains.kotlin:kotlin-stdlib", "org.jetbrains.kotlinx:kotlinx-datetime",
                        "org.jetbrains.kotlinx:kotlinx-serialization-core", "org.jetbrains.compose.runtime:runtime",
                        "org.jetbrains.compose.foundation:foundation", "org.jetbrains.compose.ui:ui",
                        "org.jetbrains.compose.components:components-resources", "org.jetbrains.compose.components:components-ui-tooling-preview",
                        "org.jetbrains.compose.annotation-internal:annotation"}
        if any(not expected_api.issubset({dep["group"] + ":" + dep["module"] for dep in variant.get("dependencies", [])}) for variant in api):
            raise RuntimeError("Calendar datetime/Native serialization API dependencies differ")
        pom = ET.fromstring((directory / (name + "-" + version + ".pom")).read_bytes())
        if any(pom.findtext("m:" + key, namespaces=ns) != expected for key, expected in (("groupId", GROUP), ("artifactId", name), ("version", version))):
            raise RuntimeError("Calendar POM coordinates differ")
        developers = [tuple(item.findtext("m:" + key, namespaces=ns) for key in ("id", "name", "url"))
                      for item in pom.findall("m:developers/m:developer", ns)]
        if developers != upstream_developers:
            raise RuntimeError("Calendar POM developers differ from upstream")
        licenses = [(item.findtext("m:name", namespaces=ns), item.findtext("m:url", namespaces=ns))
                    for item in pom.findall("m:licenses/m:license", ns)]
        config = load(HERE / "port-config.json")["modules"][MODULE]
        if licenses != [(config["license"], config["licenseUrl"])]:
            raise RuntimeError("Calendar POM license differs")
        pom_dependencies = set()
        for dep in pom.findall("m:dependencies/m:dependency", ns):
            ga = dep.findtext("m:groupId", namespaces=ns) + ":" + dep.findtext("m:artifactId", namespaces=ns)
            suffix = "-ohosx64" if target == "ohos_x64" else "-ohosarm64" if target else ""
            canonical = ga[:-len(suffix)] if suffix and ga.lower().endswith(suffix) else ga
            pom_dependencies.add(canonical)
            if (pins.get(ga) != dep.findtext("m:version", namespaces=ns) or
                    dep.findtext("m:scope", namespaces=ns) != ("compile" if target else "runtime")):
                raise RuntimeError("Calendar POM dependency or scope differs: " + ga)
        if not expected_api.issubset(pom_dependencies):
            raise RuntimeError("Calendar POM API dependency set differs")
        scopes.append({"module": name, "scope": "compile" if target else "runtime", "dependencies": sorted(pom_dependencies)})
        sources_file = directory / (name + "-" + version + "-sources.jar")
        expected_sources = {item["member"]: (item["size"], item["sha256"]) for item in model["members"] if item["compile"]}
        for item in load(HERE / "platform-inputs.json")["files"]:
            expected_sources[platform_destination(item["path"])] = (item["size"], item["sha256"])
        with zipfile.ZipFile(sources_file) as archive:
            entries = archive.infolist()
            for member, expected in expected_sources.items():
                matches = [item for item in entries if item.filename == member and not item.is_dir()]
                if len(matches) != 1:
                    raise RuntimeError("Calendar published source entry is missing or duplicated: " + member)
                data = archive.read(matches[0])
                if (len(data), digest(data)) != expected:
                    raise RuntimeError("Calendar published source bytes differ: " + member)
            licenses = [item for item in entries if item.filename == "META-INF/licenses/calendar/LICENSE.md" and not item.is_dir()]
            if len(licenses) != 1 or digest(archive.read(licenses[0])) != config["licenseSha256"]:
                raise RuntimeError("Calendar sources license differs")
        if digest((directory / (name + "-" + version + "-license.txt")).read_bytes()) != load(HERE / "port-config.json")["modules"][MODULE]["licenseSha256"]:
            raise RuntimeError("Calendar published license differs")
        if (directory / (name + "-" + version + "-provenance.json")).read_bytes() != (Path(value["work"]) / "provenance.json").read_bytes():
            raise RuntimeError("Calendar published provenance differs")
        if not target:
            build_origin = next(item for item in load(HERE / "legal/origins.json") if item["path"] == "vendor/upstream-build.gradle.kts")
            if digest((directory / (name + "-" + version + "-upstream-build.gradle.kts")).read_bytes()) != build_origin["sha256"]:
                raise RuntimeError("Calendar upstream build publication differs")
            for key, classifier in (("sources", "upstream-sources.jar"), ("pom", "upstream-pom.xml"), ("gmm", "upstream-module.json")):
                if digest((directory / (name + "-" + version + "-" + classifier)).read_bytes()) != model[key]["sha256"]:
                    raise RuntimeError("Calendar upstream publication provenance differs: " + key)
            metadata = next(item for item in stages[0]["producers"] if item["task"] == ":allMetadataJar")
            if not any(item["sha256"] == digest((directory / (name + "-" + version + ".jar")).read_bytes()) for item in metadata["outputs"]):
                raise RuntimeError("Calendar published metadata differs from compiler output")
        else:
            stage_name = "native-x64" if target == "ohos_x64" else "native-arm64"
            interop = load(Path(value["work"]) / "evidence" / (stage_name + "-interop.json"))
            if interop["fingerprint"] != value["fingerprint"] or interop["target"] != target:
                raise RuntimeError("Calendar published interop identity differs")
            compiler_task = ":compileKotlinOhosX64" if target == "ohos_x64" else ":compileKotlinOhosArm64"
            produced = next(producer["outputs"] for stage in stages for producer in stage["producers"] if producer["task"] == compiler_task)
            published = directory / (name + "-" + version + ".klib")
            packed = [item for item in produced if item["path"].endswith(".klib")]
            if packed:
                if not any(item["sha256"] == record(published)["sha256"] for item in packed):
                    raise RuntimeError("Calendar published Native Klib differs from compiler output")
            else:
                manifests = [Path(item["path"]) for item in produced if Path(item["path"]).name == "manifest" and Path(item["path"]).parent.name == "default"]
                if len(manifests) != 1:
                    raise RuntimeError("Calendar Native compiler output is ambiguous")
                output_root = manifests[0].parent.parent
                expected = {Path(item["path"]).relative_to(output_root).as_posix(): (item["size"], item["sha256"])
                            for item in produced if Path(item["path"]).is_relative_to(output_root)}
                with zipfile.ZipFile(published) as archive:
                    actual = {entry.filename: (entry.file_size, digest(archive.read(entry))) for entry in archive.infolist() if not entry.is_dir()}
                if not expected or expected != actual:
                    raise RuntimeError("Calendar published Native members differ from compiler output")
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            if path.name == "libicu.so":
                raise RuntimeError("System ICU library must not be bundled")
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as archive:
                    if any(Path(item.filename).name == "libicu.so" for item in archive.infolist()):
                        raise RuntimeError("Published archive contains system ICU library")
            if path.suffix not in (".md5", ".sha1", ".sha256", ".sha512"):
                for algorithm in ("md5", "sha1", "sha256", "sha512"):
                    checksum = path.with_name(path.name + "." + algorithm)
                    if not checksum.is_file() or checksum.read_text().strip().lower() != hashlib.new(algorithm, path.read_bytes()).hexdigest():
                        raise RuntimeError("Calendar publication checksum differs: " + str(checksum))
            files.append(record(path))
    result = {"status": "VERIFIED_STAGING_NOT_PUBLISHED", "fingerprint": value["fingerprint"], "sourceCommit": value["sourceCommit"],
              "group": GROUP, "version": version, "sysroot": value["sysrootEvidence"], "files": files, "pomScopes": scopes,
              "interops": interops, "apiEvidence": api_receipts, "upstreamComposeResourceAssets": 0,
              "link": "NOT_RUN", "runtime": "NOT_RUN"}
    destination = Path(value["work"]) / "publication.json"
    if destination.exists():
        if load(destination) != result:
            raise RuntimeError("Existing Calendar publication evidence differs")
    else:
        write(destination, result, exclusive=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("Plan", "Prepare", *TASKS, "Verify"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--tooling", type=Path)
    parser.add_argument("--native-home", type=Path)
    parser.add_argument("--sysroot", type=Path)
    parser.add_argument("--sysroot-kind", choices=("cpf", "deveco"))
    parser.add_argument("--sdk-package", type=Path)
    args = parser.parse_args()
    outputs = output_roots(args)
    work, repository = outputs
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha) or not re.fullmatch(r"[1-9][0-9]*", args.run_id) or not re.fullmatch(r"[1-9][0-9]*", args.run_attempt):
        raise RuntimeError("Full source SHA and positive run identifiers are required")
    actual = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], env=safe_environment(), check=True, capture_output=True, text=True).stdout.strip()
    if actual != args.source_sha:
        raise RuntimeError("Calendar checkout differs from requested source SHA")
    if args.stage == "Plan":
        model, members, actuals, definition = source_facts(args.source_sha, allow_uncommitted=True)
        print(json.dumps({"stage": "Plan", "sourceCommit": args.source_sha, "group": GROUP, "module": MODULE,
                          "version": "2.10.1-cpf11-preview-" + args.source_sha[:12] + "-" + args.run_id + "-" + args.run_attempt,
                          "commonSourceCount": sum(row["compile"] for row in members),
                          "actualCount": len(actuals), "platformOrigins": [row["origin"] for row in [*actuals, definition]],
                          "upstreamComposeResourceAssets": 0, "recipeCommitCheck": "NOT_RUN", "sysrootCheck": "NOT_RUN",
                          "build": "NOT_RUN", "runtime": "NOT_RUN"}))
        return
    if not args.sysroot or not args.sysroot_kind:
        raise RuntimeError("Explicit sysroot and sysroot kind are required")
    prepared = work / "prepared.json"
    tools = Path(args.tooling).resolve() if args.tooling else (work / "tooling" / TOOL_REVISION).resolve()
    value = identity(args, tools, outputs)
    verify_recipe_commit(args.source_sha, value["recipe"])
    model = load(HERE / "source-inputs.json")
    provenance = {"fingerprint": value["fingerprint"], "sourceCommit": value["sourceCommit"], "group": GROUP, "module": MODULE,
                  "version": value["version"], "toolingCommit": TOOL_REVISION, "upstreamCommit": "0e94f40b5849d92a6f1390b9e39a0d316c7d5408",
                  "sourceMembers": value["members"], "platformInputs": value["platform"], "sysroot": value["sysroot"],
                  "upstreamFiles": {key: model[key] for key in ("sources", "gmm", "pom")}, "licenseSha256": load(HERE / "port-config.json")["modules"][MODULE]["licenseSha256"]}
    fresh = args.stage == "Prepare" and not prepared.exists()
    if fresh:
        for output in outputs:
            if output.exists() and (not output.is_dir() or any(output.iterdir())):
                raise RuntimeError("Fresh work and staging must be empty: " + str(output))
        work.mkdir(parents=True, exist_ok=True)
        checked_tools(work, args.tooling)
        for item in model["members"]:
            if not item["compile"]:
                continue
            source = "compose-multiplatform/library/src/commonMain/kotlin/" + item["member"].removeprefix("commonMain/")
            data = git_bytes(args.source_sha, source)
            if (len(data), digest(data)) != (item["size"], item["sha256"]):
                raise RuntimeError("Calendar common Git source bytes changed: " + item["member"])
            target = work / "sources" / Path(*PurePosixPath(item["member"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
        for item in load(HERE / "platform-inputs.json")["files"]:
            data = git_bytes(args.source_sha, item["path"])
            if (len(data), digest(data)) != (item["size"], item["sha256"]):
                raise RuntimeError("Calendar platform Git source bytes changed: " + item["path"])
            target = work / "sources" / Path(*PurePosixPath(platform_destination(item["path"])).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
        model, members, actuals, definition = source_facts(args.source_sha, work=work)
        manifest = {"fingerprint": value["fingerprint"], "members": members, "actuals": actuals, "definition": definition,
                    "oldGeneratedMembers": [row for row in members if "/generated/resources/" in row["member"]],
                    "resources": {"package": "calendar.compose_multiplatform.library.generated.resources", "visibility": "internal",
                                  "producer": "CPF_COMPOSE_RESOURCES_PLUGIN", "upstreamAssetCount": 0},
                    "legal": record(HERE / "legal/LICENSE.md"), "origins": load(HERE / "legal/origins.json")}
        (work / "composeResources").mkdir()
        write(work / "source-manifest.json", manifest, exclusive=True)
        write(work / "provenance.json", provenance, exclusive=True)
        write(prepared, value, exclusive=True)
    if not prepared.is_file() or load(prepared) != value or load(work / "provenance.json") != provenance:
        raise RuntimeError("Calendar work identity or provenance differs; preserve it and use a fresh directory")
    tools = checked_tools(work, args.tooling)
    model, members, actuals, definition = source_facts(args.source_sha, work=work)
    expected_manifest = {"fingerprint": value["fingerprint"], "members": members, "actuals": actuals, "definition": definition,
                         "oldGeneratedMembers": [row for row in members if "/generated/resources/" in row["member"]],
                         "resources": {"package": "calendar.compose_multiplatform.library.generated.resources", "visibility": "internal",
                                       "producer": "CPF_COMPOSE_RESOURCES_PLUGIN", "upstreamAssetCount": 0},
                         "legal": record(HERE / "legal/LICENSE.md"), "origins": load(HERE / "legal/origins.json")}
    if load(work / "source-manifest.json") != expected_manifest:
        raise RuntimeError("Calendar prepared source manifest differs")
    if args.stage == "Prepare":
        print(json.dumps({"work": str(work), "toolingHome": str(tools), "fingerprint": value["fingerprint"], "version": value["version"]}))
        return
    check_native(args.native_home)
    if args.stage == "Verify":
        print(json.dumps(verify_publication(value, args.native_home)))
        return
    if args.stage != "Tasks":
        discovery = load(work / "task-discovery.json")
        if discovery["fingerprint"] != value["fingerprint"] or record(discovery["stdout"]["path"]) != discovery["stdout"]:
            raise RuntimeError("Calendar task discovery differs")
    if args.stage not in ("Tasks", "Resolve"):
        if load(work / "evidence/resolve.json")["fingerprint"] != value["fingerprint"]:
            raise RuntimeError("Calendar resolution belongs to another recipe")
        for label, stage, target in COMPILES:
            if label == args.stage:
                break
            verify_stage(value, stage, target)
    if args.stage == "Publish":
        for label, stage, target in COMPILES[1:]:
            verify_native_interop(value, label)
            verify_api(value, stage, target, args.native_home)
        if repository.exists() and (not repository.is_dir() or any(repository.iterdir())):
            raise RuntimeError("Staging is not empty; refusing to overwrite existing files")
    stage_file = {"Tasks": "task-discovery.json", "Resolve": "evidence/resolve.json", "Metadata": "evidence/metadata.json",
                  "NativeX64": "evidence/native-x64.json", "NativeArm64": "evidence/native-arm64.json"}.get(args.stage)
    if stage_file and (work / stage_file).exists():
        raise RuntimeError("Calendar stage evidence already exists; use a fresh work directory")
    native_stage = {"NativeX64": "native-x64", "NativeArm64": "native-arm64"}.get(args.stage)
    if native_stage and any((work / "evidence" / (native_stage + suffix)).exists() for suffix in ("-interop.json", "-api.json")):
        raise RuntimeError("Calendar Native evidence already exists; use a fresh work directory")
    wrapper = ["cmd.exe", "/d", "/c", str(HERE / "gradlew.bat")] if os.name == "nt" else ["sh", str(HERE / "gradlew")]
    argv = [*wrapper, "-p", str(HERE), "--project-cache-dir", str(work / "project-cache"),
            "-PportWork=" + str(work), "-PportTooling=" + str(tools), "-PportFingerprint=" + value["fingerprint"],
            "-PportVersion=" + value["version"], "-PportRepository=" + str(repository), "-PcalendarSysroot=" + value["sysrootPath"],
            "-Pkotlin.project.persistent.dir=" + str(work / "kotlin-cache"), "-Pkotlin.native.home=" + str(args.native_home.resolve()),
            "--console=plain", "--stacktrace", *TASKS[args.stage]]
    logs = work / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stdout, stderr = logs / (args.stage + ".stdout.log"), logs / (args.stage + ".stderr.log")
    with stdout.open("xb") as out, stderr.open("xb") as err:
        run = subprocess.run(argv, env=safe_environment(work), stdout=out, stderr=err)
    if run.returncode:
        print(stderr.read_text(encoding="utf-8", errors="replace")[-16384:], file=sys.stderr)
        raise RuntimeError("Gradle " + args.stage + " failed with exit " + str(run.returncode))
    if args.stage == "Tasks":
        names = {name.lstrip(":") for name in re.findall(r"^\s*([\w:-]+)(?:\s+-|\s*$)", stdout.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)}
        if not REQUIRED.issubset(names):
            raise RuntimeError("Calendar tasks absent from actual discovery: " + ", ".join(sorted(REQUIRED - names)))
        write(work / "task-discovery.json", {"fingerprint": value["fingerprint"], "required": sorted(REQUIRED), "stdout": record(stdout)}, exclusive=True)
    for label, stage, target in COMPILES:
        if label == args.stage:
            verify_stage(value, stage, target)
            if target is not None:
                verify_native_interop(value, args.stage, create=True)
                signature_stage(value, stage, target, args.run_attempt, args.native_home)
    print(json.dumps({"stage": args.stage, "exitCode": run.returncode, "fingerprint": value["fingerprint"], "stdout": record(stdout)}))


if __name__ == "__main__":
    main()
