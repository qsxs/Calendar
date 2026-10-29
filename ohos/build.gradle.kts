import groovy.json.JsonOutput
import groovy.json.JsonSlurper
import java.security.MessageDigest
import org.gradle.api.Task
import org.gradle.api.artifacts.Configuration
import org.gradle.api.artifacts.component.ModuleComponentIdentifier
import org.gradle.api.artifacts.result.ResolvedDependencyResult
import org.gradle.api.artifacts.result.UnresolvedDependencyResult
import org.gradle.api.publish.maven.MavenPublication
import org.gradle.api.publish.maven.tasks.PublishToMavenRepository
import org.gradle.jvm.tasks.Jar
import org.jetbrains.kotlin.gradle.plugin.mpp.KotlinNativeTarget
import org.jetbrains.kotlin.gradle.tasks.KotlinNativeCompile

plugins {
    kotlin("multiplatform")
    kotlin("plugin.compose")
    kotlin("plugin.serialization")
    id("org.jetbrains.compose")
    `maven-publish`
}
fun readMap(path: File) = (JsonSlurper().parse(path) as Map<*, *>).entries.associate { it.key.toString() to it.value }
fun asMap(value: Any?) = (value as Map<*, *>).entries.associate { it.key.toString() to requireNotNull(it.value) }
fun sha(path: File) = MessageDigest.getInstance("SHA-256").digest(path.readBytes()).joinToString("") { "%02x".format(it) }
fun leaves(path: File): List<File> = if (path.isDirectory) path.walkTopDown().filter(File::isFile).toList() else listOf(path)
fun records(files: Iterable<File>) = files.filter(File::isFile).distinct().sortedBy { it.path }.map {
    mapOf("path" to it.absolutePath, "size" to it.length(), "sha256" to sha(it))
}
val config = readMap(file("port-config.json"))
val model = asMap(asMap(config.getValue("modules")).getValue("compose-multiplatform"))
val sourceInputs = readMap(file("source-inputs.json"))
val platformInputs = readMap(file("platform-inputs.json"))
val sysrootInputs = asMap(readMap(file("sysroot-input.json")).getValue("candidate"))
val pins = readMap(file("dependency-pins.json"))
val metadataPins = readMap(file("metadata-pins.json"))
val compilerPluginTools = readMap(file("compiler-plugin-toolchain.json"))
val compilerPluginPins = asMap(compilerPluginTools.getValue("pins"))
val compilerPluginArtifacts = asMap(compilerPluginTools.getValue("artifacts"))
val toolRoot = file(providers.gradleProperty("portTooling").get()).resolve("toolchains/kotlin-native/2.4.20-0.1.0-08")
val metadataTools = readMap(toolRoot.resolve("metadata-toolchain.json"))
val commonizerTools = readMap(toolRoot.resolve("commonizer-toolchain.json"))
val nativeTools = readMap(toolRoot.resolve("native-compiler-toolchain.json"))
val commonizerPins = asMap(metadataTools.getValue("launcherPins")) + asMap(commonizerTools.getValue("pins"))
val commonizerArtifacts = asMap(metadataTools.getValue("launcherArtifacts")) + asMap(commonizerTools.getValue("artifacts"))
val nativePins = asMap(nativeTools.getValue("pins"))
val nativeArtifacts = asMap(nativeTools.getValue("artifacts"))
val fingerprint = providers.gradleProperty("portFingerprint").get()
val work = file(providers.gradleProperty("portWork").get()).canonicalFile
check(fingerprint.matches(Regex("[0-9a-f]{64}")))
val sourceRoot = work.resolve("sources").canonicalFile
val sysroot = file(providers.gradleProperty("calendarSysroot").get()).canonicalFile
val manifest = readMap(work.resolve("source-manifest.json"))
check(manifest["fingerprint"] == fingerprint)
group = model.getValue("group").toString()
version = providers.gradleProperty("portVersion").get()
layout.buildDirectory.set(work.resolve("build"))
kotlin {
    ohosX64()
    ohosArm64()
    compilerOptions.freeCompilerArgs.add("-Xexpect-actual-classes")
    sourceSets {
        val commonMain by getting {
            // 完整 41 手写 common 源原字节编译；旧生成源留在原 JAR/展开树，不重复声明。
            kotlin.srcDir(sourceRoot.resolve("commonMain"))
            dependencies {
                implementation(compose.runtime)
                implementation(compose.foundation)
                implementation(compose.ui)
                implementation(compose.components.resources)
                implementation(compose.components.uiToolingPreview)
                // P5-CALENDAR-AUTO-01：所需 IntRange 的 FQN/参数/默认值已核实，保持 implementation scope。
                implementation("org.jetbrains.compose.annotation-internal:annotation:1.9.1-0.1.0-11")
                implementation("org.jetbrains.kotlinx:kotlinx-serialization-core:1.11.0-0.1.0-07")
                api("org.jetbrains.kotlinx:kotlinx-datetime:0.8.0-0.1.0-07")
            }
        }
        val ohosMain by creating {
            dependsOn(commonMain)
            kotlin.srcDir(sourceRoot.resolve("ohosMain"))
            // 对齐上游 nonJvmMain 对所有 Native 目标公开 serialization 的 scope。
            dependencies { api("org.jetbrains.kotlinx:kotlinx-serialization-core:1.11.0-0.1.0-07") }
        }
        listOf("ohosX64Main", "ohosArm64Main").forEach { getByName(it).dependsOn(ohosMain) }
        matching { it.name.endsWith("Test") }.configureEach { kotlin.setSrcDirs(emptyList<File>()) }
    }
    targets.withType<KotlinNativeTarget>().configureEach {
        compilations.getByName("main").cinterops.create("calendarIcu") {
            definitionFile.set(sourceRoot.resolve("nativeInterop/cinterop/calendar_icu.def"))
            includeDirs(sysroot.resolve("usr/include"))
        }
    }
}
compose.resources {
    customDirectory("commonMain", providers.provider { layout.projectDirectory.dir(work.resolve("composeResources").absolutePath) })
    packageOfResClass = "calendar.compose_multiplatform.library.generated.resources"
    publicResClass = false
    generateResClass = always
}
configurations.configureEach {
    if (name == nativeTools["configuration"] || name == commonizerTools["configuration"]) {
        val fixedPins = if (name == nativeTools["configuration"]) nativePins else commonizerPins
        resolutionStrategy.eachDependency {
            val ga = "${requested.group}:${requested.name}"
            val fixed = fixedPins[ga]?.toString() ?: error("Unpinned Calendar compiler host: $ga")
            if (requested.version != fixed) useVersion(fixed)
        }
    }
    val metadata = name.endsWith("ResolvableDependenciesMetadata") || name == "allSourceSetsCompileDependenciesMetadata"
    val runtime = metadata || name.endsWith("CompileKlibraries") || name.endsWith("CompilationDependenciesMetadata")
    if (runtime && !name.contains("Test", ignoreCase = true)) resolutionStrategy.eachDependency {
        val ga = "${requested.group}:${requested.name}"
        val fixed = (if (metadata) metadataPins[ga] ?: pins[ga] else pins[ga])?.toString()
            ?: error("Unpinned Calendar dependency: $requested")
        if (requested.version != fixed) useVersion(fixed)
    }
}
fun evidence(name: String, value: Any) {
    val target = work.resolve("evidence/$name.json")
    target.parentFile.mkdirs()
    target.writeText(JsonOutput.prettyPrint(JsonOutput.toJson(value)) + "\n")
}
fun edges(configuration: Configuration) = configuration.incoming.resolutionResult.allDependencies.map { edge ->
    if (edge !is ResolvedDependencyResult) throw GradleException("Unresolved Calendar dependency: $edge", (edge as? UnresolvedDependencyResult)?.failure)
    mapOf("from" to edge.from.id.displayName, "requested" to edge.requested.displayName, "selected" to edge.selected.id.displayName)
}
fun checkedTools(name: String, fixed: Map<String, Any>, expected: Map<String, Any>): Map<String, Any> {
    val configuration = configurations.getByName(name)
    val artifacts = configuration.resolvedConfiguration.resolvedArtifacts.map { artifact ->
        val id = artifact.moduleVersion.id
        val ga = "${id.group}:${id.name}"
        check(fixed[ga] == id.version) { "Host tool version differs: $id" }
        val item = asMap(expected.getValue(ga))
        check(sha(artifact.file) == item["sha256"] && artifact.file.length() == (item.getValue("size") as Number).toLong()) { "Host tool bytes differ: $id" }
        mapOf("gav" to id.toString(), "files" to records(listOf(artifact.file)))
    }
    check(artifacts.map { it.getValue("gav") }.toSet() == fixed.map { "${it.key}:${it.value}" }.toSet()) { "Host tool set differs: $name" }
    return mapOf("configuration" to name, "edges" to edges(configuration), "artifacts" to artifacts)
}
fun toolRecords() = mapOf("commonizer" to checkedTools(commonizerTools.getValue("configuration").toString(), commonizerPins, commonizerArtifacts),
    "nativeCompiler" to checkedTools(nativeTools.getValue("configuration").toString(), nativePins, nativeArtifacts))

fun toolchain() = mapOf(
    "hostTools" to toolRecords(),
    "gradle" to gradle.gradleVersion,
    "javaVersion" to System.getProperty("java.version"),
    "javaRuntimeVersion" to System.getProperty("java.runtime.version"),
    "javaHome" to System.getProperty("java.home"),
    "nativeHome" to providers.gradleProperty("kotlin.native.home").get(),
    "konanDataDir" to System.getenv("KONAN_DATA_DIR"),
    "gradleUserHome" to gradle.gradleUserHomeDir.absolutePath,
    "temp" to System.getenv("TEMP"),
    "plugins" to buildscript.configurations.getByName("classpath").resolvedConfiguration.resolvedArtifacts.map {
        mapOf("component" to it.moduleVersion.id.toString(), "files" to records(listOf(it.file)))
    },
)

fun resolution() = listOf("commonMainResolvableDependenciesMetadata", "ohosX64CompileKlibraries", "ohosArm64CompileKlibraries").map { name ->
    val configuration = configurations.getByName(name)
    val metadata = name.endsWith("ResolvableDependenciesMetadata")
    mapOf("configuration" to name, "edges" to configuration.incoming.resolutionResult.allDependencies.map { edge ->
        if (edge !is ResolvedDependencyResult) throw GradleException("Unresolved Calendar dependency: $edge", (edge as? UnresolvedDependencyResult)?.failure)
        mapOf("requested" to edge.requested.displayName, "selected" to edge.selected.id.displayName)
    }, "artifacts" to configuration.incoming.artifacts.artifacts.map { artifact ->
        val id = artifact.id.componentIdentifier
        check(id is ModuleComponentIdentifier)
        val ga = "${id.group}:${id.module}"
        check((if (metadata) metadataPins[ga] ?: pins[ga] else pins[ga]) == id.version)
        val attrs = artifact.variant.attributes.keySet().associate { it.name to artifact.variant.attributes.getAttribute(it).toString() }
        if (metadataPins.containsKey(ga)) {
            val common = attrs["org.gradle.usage"] == "kotlin-metadata" && attrs["org.jetbrains.kotlin.platform.type"] == "common"
            val javaAlias = ga == "androidx.arch.core:core-common" && id.version == "2.2.0" &&
                attrs["org.gradle.usage"] in listOf("java-api", "java-runtime") &&
                attrs["org.jetbrains.kotlin.platform.type"] in listOf(null, "jvm") && attrs["org.gradle.category"] == "library"
            check(metadata && (common || javaAlias) && attrs["org.jetbrains.kotlin.native.target"] == null) {
                "Calendar metadata supplement selected outside common metadata: $id"
            }
        }
        mapOf("component" to id.displayName, "attributes" to attrs, "files" to records(leaves(artifact.file)))
    })
}
tasks.register("resolveCalendar") {
    doLast {
        check(gradle.gradleVersion == config.getValue("gradle") && System.getProperty("java.specification.version") == "17")
        listOf("compileCommonMainKotlinMetadata", "allMetadataJar", "compileKotlinOhosX64", "compileKotlinOhosArm64",
            "cinteropCalendarIcuOhosX64", "cinteropCalendarIcuOhosArm64").forEach { tasks.getByName(it) }
        evidence("resolve", mapOf("fingerprint" to fingerprint, "toolchain" to toolchain(), "configurations" to resolution()))
    }
}
fun recordStage(name: String, target: String?) {
    tasks.register(name) {
        val compilerName = if (target == null) "compileCommonMainKotlinMetadata" else "compileKotlin$target"
        dependsOn(compilerName)
        if (target == null) dependsOn("allMetadataJar")
        outputs.upToDateWhen { false }
        doLast {
            val compiler = tasks.getByName(compilerName) as KotlinNativeCompile
            val sources = compiler.sources.files.filter { it.isFile && it.extension == "kt" }.map { it.canonicalFile }.toSet()
            val pluginIds = listOf("org.jetbrains.kotlin:kotlin-compose-compiler-plugin-embeddable",
                "org.jetbrains.kotlin:kotlin-serialization-compiler-plugin-embeddable")
            val expectedPlugins = pluginIds.map { id ->
                check(compilerPluginPins[id] == config["kotlin"])
                val item = asMap(compilerPluginArtifacts.getValue(id))
                (item.getValue("size") as Number).toLong() to item.getValue("sha256").toString()
            }.toSet()
            val pluginFiles = compiler.compilerPluginClasspath?.files.orEmpty()
            check(expectedPlugins.size == pluginIds.size && pluginFiles.size == pluginIds.size &&
                pluginFiles.map { it.length() to sha(it) }.toSet() == expectedPlugins) { "Calendar compiler plugin set differs" }
            // 从配方锁定清单计算编译输入，不能以可变工作区 manifest 充当来源真值。
            val commonInputs = (sourceInputs.getValue("members") as List<*>).map(::asMap).filter { it["compile"] == true }
            val expectedCommon = commonInputs.associate { item ->
                sourceRoot.resolve(item.getValue("member").toString()).canonicalFile to item.getValue("sha256").toString()
            }
            val platformPrefix = "compose-multiplatform/library/src/ohosMain/kotlin/"
            val expectedActuals = (platformInputs.getValue("files") as List<*>).map(::asMap)
                .filter { it.getValue("path").toString().endsWith(".kt") }.associate { item ->
                    val relative = item.getValue("path").toString()
                    check(relative.startsWith(platformPrefix))
                    sourceRoot.resolve("ohosMain/" + relative.removePrefix(platformPrefix)).canonicalFile to item.getValue("sha256").toString()
                }
            val expected = expectedCommon + if (target == null) emptyMap() else expectedActuals
            check(expected.size == if (target == null) 41 else 43)
            check(sources.containsAll(expected.keys) && expected.all { (path, hash) -> sha(path) == hash }) { "Calendar source bytes or source closure differ" }
            val additional = sources - expected.keys
            check(additional.isNotEmpty() && additional.all { it.toPath().startsWith(layout.buildDirectory.dir("generated/compose").get().asFile.toPath()) }) {
                "Only formal Compose generated resource sources may supplement Calendar source closure"
            }
            val generatedText = additional.joinToString("\n") { it.readText() }
            listOf("allDrawableResources", "allStringResources", "allStringArrayResources", "allPluralStringResources", "allFontResources").forEach {
                check(it in generatedText) { "Formal Calendar resource collector absent: $it" }
            }
            check(additional.any { it.name == "Res.kt" } && additional.any { it.name == "ExpectResourceCollectors.kt" })
            if (target != null) check(additional.any { it.name == "ActualResourceCollectors.kt" })
            val dependencies = linkedSetOf<Task>()
            fun collect(task: Task) {
                task.taskDependencies.getDependencies(task).forEach { if (dependencies.add(it)) collect(it) }
            }
            collect(compiler)
            val resourceProducers = dependencies.filter { producer -> producer.name.startsWith("generate") &&
                (producer.name.contains("Resource") || producer.name == "generateComposeResClass") && additional.any { source ->
                    producer.outputs.files.files.any { output -> source == output.canonicalFile ||
                        (output.isDirectory && source.toPath().startsWith(output.canonicalFile.toPath())) }
                } }
            check(resourceProducers.isNotEmpty())
            additional.forEach { source ->
                check(resourceProducers.any { producer -> producer.outputs.files.files.any { output ->
                    source == output.canonicalFile || (output.isDirectory && source.toPath().startsWith(output.canonicalFile.toPath()))
                } }) { "Generated Calendar resource source has no real producer dependency: $source" }
            }
            val producers = listOf(compiler) + if (target == null) listOf(tasks.getByName("allMetadataJar")) else
                listOf(tasks.getByName("cinteropCalendarIcu$target"))
            (producers + resourceProducers).distinct().forEach { producer ->
                val state = producer.state
                check(state.executed && producer.enabled && !state.noSource && state.failure == null &&
                    state.skipMessage in listOf(null, "UP-TO-DATE", "FROM-CACHE")) { "Calendar producer did not execute: ${producer.path}" }
                check(records(producer.outputs.files.files.flatMap(::leaves)).isNotEmpty())
            }
            val stage = if (target == null) "metadata" else if (target == "OhosX64") "native-x64" else "native-arm64"
            evidence(stage, mapOf("fingerprint" to fingerprint, "stage" to stage, "toolchain" to toolchain(), "sysroot" to sysroot.absolutePath,
                "sourceManifest" to records(listOf(work.resolve("source-manifest.json"))).single(),
                "interopDefinition" to if (target == null) null else records(listOf(sourceRoot.resolve("nativeInterop/cinterop/calendar_icu.def"))).single(),
                "interopHeaders" to if (target == null) emptyList<Map<String, Any>>() else records(
                    (sysrootInputs.getValue("files") as List<*>).map(::asMap).map { it.getValue("path").toString() }
                        .filter { it.startsWith("usr/include/") }.map { sysroot.resolve(it) }),
                "handwrittenCommonCount" to 41, "actualCount" to if (target == null) 0 else 2,
                "sourceDependencies" to listOf("commonImplementation" to "commonMainImplementation", "commonApi" to "commonMainApi", "ohosApi" to "ohosMainApi").associate { (scope, name) ->
                    scope to configurations.getByName(name).dependencies.map { "${it.group}:${it.name}" }.filter { it != "org.jetbrains.kotlin:kotlin-stdlib" }
                },
                "sources" to records(sources), "generatedResources" to records(additional),
                "resourceProducers" to resourceProducers.map { mapOf("task" to it.path, "outputs" to records(it.outputs.files.files.flatMap(::leaves))) },
                "compilerPlugins" to records(compiler.compilerPluginClasspath?.files.orEmpty()), "pluginOptions" to compiler.compilerPluginCommandLine,
                "directDependencies" to compiler.taskDependencies.getDependencies(compiler).map { it.path }.sorted(),
                "producers" to producers.map { mapOf("task" to it.path, "outputs" to records(it.outputs.files.files.flatMap(::leaves))) },
                "configurations" to resolution(), "runtime" to "NOT_RUN"))
        }
    }
}
recordStage("recordCalendarMetadata", null)
recordStage("recordCalendarX64", "OhosX64")
recordStage("recordCalendarArm64", "OhosArm64")
publishing {
    repositories { maven { name = "Port"; url = uri(providers.gradleProperty("portRepository").get()) } }
    publications.withType<MavenPublication>().configureEach {
        artifact(file("legal/LICENSE.md")) { classifier = "license"; extension = "txt" }
        artifact(work.resolve("provenance.json")) { classifier = "provenance"; extension = "json" }
        if (name == "kotlinMultiplatform") {
            artifact(file("vendor/compose-multiplatform-2.10.1-sources.jar")) { classifier = "upstream-sources"; extension = "jar" }
            artifact(file("vendor/compose-multiplatform-2.10.1.pom")) { classifier = "upstream-pom"; extension = "xml" }
            artifact(file("vendor/compose-multiplatform-2.10.1.module")) { classifier = "upstream-module"; extension = "json" }
            artifact(file("vendor/upstream-build.gradle.kts")) { classifier = "upstream-build"; extension = "gradle.kts" }
        }
        pom {
            name.set("Kizitonwose Calendar Compose Multiplatform")
            description.set("Unmodified 2.10.1 common sources with CPF OHOS dual ABI actuals")
            licenses { license { name.set("MIT License"); url.set(model.getValue("licenseUrl").toString()) } }
            developers { developer { id.set("kizitonwose"); name.set("Kizito Nwose"); url.set("https://github.com/kizitonwose") } }
            scm { url.set(model.getValue("repository").toString()) }
        }
    }
}
tasks.withType<PublishToMavenRepository>().configureEach {
    doFirst {
        check(sha(file("legal/LICENSE.md")) == model.getValue("licenseSha256"))
        check(readMap(work.resolve("provenance.json"))["fingerprint"] == fingerprint)
        listOf("metadata", "native-x64", "native-arm64").forEach { check(readMap(work.resolve("evidence/$it.json"))["fingerprint"] == fingerprint) }
        listOf("native-x64", "native-arm64").forEach { check(readMap(work.resolve("evidence/$it-interop.json"))["fingerprint"] == fingerprint) }
    }
}
tasks.withType<Jar>().configureEach {
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    from(file("legal")) { into("META-INF/licenses/calendar") }
    from(work.resolve("source-manifest.json")) { into("META-INF/upstream") }
}
tasks.register("publishCalendar") { dependsOn("publishAllPublicationsToPortRepository") }

// 标准 sources artifact 保留全部原始 common、OHOS actual 与 ICU 定义。
tasks.withType<Jar>().matching { it.name.endsWith("SourcesJar", ignoreCase = true) }.configureEach {
    duplicatesStrategy = DuplicatesStrategy.EXCLUDE
    from(sourceRoot) { include("**/*.kt", "**/*.def") }
}
