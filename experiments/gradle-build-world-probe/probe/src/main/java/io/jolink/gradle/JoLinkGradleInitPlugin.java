package io.jolink.gradle;

import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermission;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.EnumSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

import org.gradle.api.Action;
import org.gradle.api.GradleException;
import org.gradle.api.Plugin;
import org.gradle.api.Project;
import org.gradle.api.Task;
import org.gradle.api.invocation.Gradle;
import org.gradle.api.tasks.SourceSet;
import org.gradle.api.tasks.SourceSetContainer;
import org.gradle.api.tasks.compile.JavaCompile;
import org.gradle.api.tasks.testing.Test;
import org.gradle.api.tasks.testing.junit.JUnitOptions;
import org.gradle.api.tasks.testing.junitplatform.JUnitPlatformOptions;
import org.gradle.api.tasks.testing.testng.TestNGOptions;
import org.gradle.jvm.toolchain.JavaCompiler;
import org.gradle.jvm.toolchain.JavaLauncher;

/** Private init-plugin spike that exports facts from evaluated Gradle tasks. */
public final class JoLinkGradleInitPlugin implements Plugin<Gradle> {
    private static final String SCHEMA = "jolink.gradle-build-world-probe.v1";
    private static final String VERSION = "0.1.0-spike4";
    private final Map<String, Map<String, String>> baselineEnvironments =
            new LinkedHashMap<>();
    private String requestId;
    private String probeSha256;
    private String exportTaskName;
    private String exportScope;

    private static final class BoundaryException extends RuntimeException {
        final String code;

        BoundaryException(String code, String message) {
            super(code + ": " + message);
            this.code = code;
        }
    }

    @Override
    public void apply(final Gradle gradle) {
        requestId = requiredIdentity("jolink.gradle.requestId", 8, 128);
        probeSha256 = requiredIdentity("jolink.gradle.probeSha256", 64, 64);
        if (!probeSha256.matches("[0-9a-f]{64}")) {
            throw new GradleException("GRADLE_PROBE_IDENTITY_INVALID: probe SHA");
        }
        exportTaskName = "jolinkExportBuildWorld_"
                + probeSha256.substring(0, 12);
        exportScope = System.getProperty("jolink.gradle.scope", "test");
        if (!exportScope.equals("test") && !exportScope.equals("runtime")) {
            throw new GradleException(
                    "GRADLE_PROBE_SCOPE_INVALID: " + exportScope);
        }
        final String targetPath = System.getProperty(
                "jolink.gradle.targetProject", ":");
        gradle.beforeProject(new Action<Project>() {
            @Override
            public void execute(final Project project) {
                if (!targetPath.equals(project.getPath())) {
                    return;
                }
                project.getPluginManager().withPlugin(
                        "java",
                        ignored -> configureProject(project));
            }
        });
    }

    private void configureProject(final Project project) {
        if (exportScope.equals("test")) {
            project.getTasks().withType(Test.class).all(task ->
                    baselineEnvironments.put(
                            task.getPath(), stringify(task.getEnvironment())));
        }
        registerExportTask(project);
        registerSlowCompileGate(project);
    }

    private void registerSlowCompileGate(final Project project) {
        long slowMillis = Long.parseLong(System.getProperty(
                "jolink.gradle.slowCompileMillis", "0"));
        if (slowMillis <= 0) {
            return;
        }
        String taskName = "jolinkSlowCompileGate_"
                + probeSha256.substring(0, 12);
        if (project.getTasks().findByName(taskName) != null) {
            throw new GradleException(
                    "GRADLE_PROBE_TASK_CONFLICT: " + taskName);
        }
        Task gate = project.getTasks().create(taskName);
        gate.doLast(ignored -> {
            try {
                Path marker = outputPath().resolveSibling(
                        outputPath().getFileName() + ".started");
                Files.createDirectories(marker.getParent());
                Files.write(marker, "started\n".getBytes(StandardCharsets.UTF_8));
                restrict(marker);
                Thread.sleep(Math.min(slowMillis, 120_000L));
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                throw new GradleException("GRADLE_PROBE_SLOW_GATE_INTERRUPTED");
            } catch (IOException error) {
                throw new GradleException("GRADLE_PROBE_SLOW_GATE_FAILED", error);
            }
        });
        project.getTasks().getByName("compileJava").dependsOn(gate);
    }

    private void registerExportTask(final Project project) {
        if (project.getTasks().findByName(exportTaskName) != null) {
            throw new GradleException(
                    "GRADLE_PROBE_TASK_CONFLICT: " + exportTaskName);
        }
        Task task = project.getTasks().create(exportTaskName);
        task.setGroup("joLink");
        task.setDescription("Export a private task-native Java Build World.");
        if (exportScope.equals("test")) {
            task.dependsOn(Arrays.asList("classes", "testClasses"));
        }
        task.doLast(ignored -> export(project));
    }

    private void export(Project project) {
        Path output = outputPath();
        Path started = output.resolveSibling(output.getFileName() + ".started");
        try {
            Files.createDirectories(output.getParent());
            Files.write(started, "started\n".getBytes(StandardCharsets.UTF_8));
            restrict(started);
            try {
                writePrivate(output, render(project));
            } catch (BoundaryException boundary) {
                Map<String, Object> failure = new LinkedHashMap<>();
                failure.put("ok", false);
                failure.put("schema", SCHEMA);
                failure.put("probeVersion", VERSION);
                failure.put("requestId", requestId);
                failure.put("probeSha256", probeSha256);
                failure.put("targetProjectPath", project.getPath());
                failure.put("errorCode", boundary.code);
                failure.put("message", boundary.getMessage());
                writePrivate(output, json(failure) + "\n");
                throw boundary;
            }
        } catch (Exception error) {
            throw new RuntimeException("Unable to export joLink Build World.", error);
        }
    }

    private static Path outputPath() {
        String rawOutput = System.getProperty("jolink.gradle.output");
        if (rawOutput == null || rawOutput.trim().isEmpty()) {
            throw new IllegalArgumentException("Missing jolink.gradle.output.");
        }
        return new File(rawOutput).toPath().toAbsolutePath().normalize();
    }

    private static void writePrivate(Path output, String content)
            throws IOException {
        Path temporary = output.resolveSibling(
                "." + output.getFileName() + ".tmp");
        Files.write(temporary, content.getBytes(StandardCharsets.UTF_8));
        restrict(temporary);
        try {
            Files.move(
                    temporary,
                    output,
                    StandardCopyOption.ATOMIC_MOVE,
                    StandardCopyOption.REPLACE_EXISTING);
        } catch (AtomicMoveNotSupportedException ignored) {
            Files.move(
                    temporary,
                    output,
                    StandardCopyOption.REPLACE_EXISTING);
        }
        restrict(output);
    }

    private String render(Project project) throws Exception {
        SourceSetContainer sourceSets = project.getExtensions()
                .getByType(SourceSetContainer.class);
        if (exportScope.equals("runtime")) {
            return renderRuntime(project, sourceSets);
        }
        return renderTest(project, sourceSets);
    }

    private String renderRuntime(
            Project project, SourceSetContainer sourceSets) throws Exception {
        validateRuntimeBoundaries(project, sourceSets);
        SourceSet main = sourceSets.getByName(SourceSet.MAIN_SOURCE_SET_NAME);
        Task rawCompileJava = project.getTasks()
                .getByName(main.getCompileJavaTaskName());
        if (!(rawCompileJava instanceof JavaCompile)) {
            throw boundary(
                    "GRADLE_COMPILE_TASK_UNSUPPORTED",
                    "Expected compileJava to be a JavaCompile task.");
        }
        JavaCompile compileJava = (JavaCompile) rawCompileJava;
        Map<String, Object> root = commonModel(project);
        root.put("main", sourceSet(main));
        root.put("compileJava", compileTask(compileJava));
        root.put("runtimeExecution", runtimeExecution(
                project, main, compileJava));
        return json(root) + "\n";
    }

    private String renderTest(
            Project project, SourceSetContainer sourceSets) throws Exception {
        validateTestBoundaries(project, sourceSets);
        SourceSet main = sourceSets.getByName(SourceSet.MAIN_SOURCE_SET_NAME);
        SourceSet test = sourceSets.getByName(SourceSet.TEST_SOURCE_SET_NAME);
        Task rawCompileJava = project.getTasks()
                .getByName(main.getCompileJavaTaskName());
        Task rawCompileTestJava = project.getTasks()
                .getByName(test.getCompileJavaTaskName());
        if (!(rawCompileJava instanceof JavaCompile)
                || !(rawCompileTestJava instanceof JavaCompile)) {
            throw boundary(
                    "GRADLE_COMPILE_TASK_UNSUPPORTED",
                    "Expected compileJava and compileTestJava to be JavaCompile tasks.");
        }
        JavaCompile compileJava = (JavaCompile) rawCompileJava;
        JavaCompile compileTestJava = (JavaCompile) rawCompileTestJava;
        Test testTask = (Test) project.getTasks().getByName("test");

        Map<String, Object> root = commonModel(project);
        root.put("main", sourceSet(main));
        root.put("test", sourceSet(test));
        root.put("compileJava", compileTask(compileJava));
        root.put("compileTestJava", compileTask(compileTestJava));
        root.put("testRuntime", testTask(testTask));
        return json(root) + "\n";
    }

    private Map<String, Object> commonModel(Project project) throws Exception {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("ok", true);
        root.put("schema", SCHEMA);
        root.put("probeVersion", VERSION);
        root.put("requestId", requestId);
        root.put("probeSha256", probeSha256);
        root.put("exportTaskName", exportTaskName);
        root.put("targetProjectPath", project.getPath());
        root.put("exportScope", exportScope);
        root.put("gradleVersion", project.getGradle().getGradleVersion());
        root.put("projectPath", project.getPath());
        root.put("projectName", project.getName());
        root.put("projectDirectory", canonical(project.getProjectDir()));
        root.put("rootDirectory", canonical(project.getRootDir()));
        root.put("gradleDaemonJavaHome", canonical(new File(
                System.getProperty("java.home"))));
        root.put("gradleDaemonJavaVersion", System.getProperty(
                "java.specification.version"));
        List<String> pluginClasses = new ArrayList<>();
        for (Plugin<?> plugin : project.getPlugins()) {
            pluginClasses.add(plugin.getClass().getName());
        }
        Collections.sort(pluginClasses);
        root.put("appliedPluginClassNames", pluginClasses);
        return root;
    }

    private static void validateProjectBoundary(Project project) {
        if (project.getRootProject().getAllprojects().size() != 1) {
            throw boundary(
                    "GRADLE_MULTI_PROJECT_UNSUPPORTED",
                    "The product Probe requires exactly one Gradle Project.");
        }
    }

    private static void validateRuntimeBoundaries(
            Project project, SourceSetContainer sourceSets) {
        validateProjectBoundary(project);
        if (sourceSets.findByName(SourceSet.MAIN_SOURCE_SET_NAME) == null) {
            throw boundary(
                    "GRADLE_SOURCE_SET_UNSUPPORTED",
                    "Runtime scope requires the main SourceSet.");
        }
    }

    private static void validateTestBoundaries(
            Project project, SourceSetContainer sourceSets) {
        validateProjectBoundary(project);
        Set<String> sourceSetNames = new LinkedHashSet<>();
        for (SourceSet sourceSet : sourceSets) {
            sourceSetNames.add(sourceSet.getName());
        }
        Set<String> expectedSourceSets = new LinkedHashSet<>(
                Arrays.asList("main", "test"));
        if (!sourceSetNames.equals(expectedSourceSets)) {
            throw boundary(
                    "GRADLE_SOURCE_SET_UNSUPPORTED",
                    "G1.1 requires exactly main and test SourceSets.");
        }
        Set<String> testTaskNames = new LinkedHashSet<>();
        for (Test test : project.getTasks().withType(Test.class)) {
            testTaskNames.add(test.getName());
        }
        if (!testTaskNames.equals(Collections.singleton("test"))) {
            throw boundary(
                    "GRADLE_TEST_TASK_UNSUPPORTED",
                    "G1.1 requires exactly the default test Test task.");
        }
    }

    private Map<String, Object> runtimeExecution(
            Project project,
            SourceSet main,
            JavaCompile compileJava) throws IOException {
        Map<String, Object> result = new LinkedHashMap<>();
        Task classes = project.getTasks().getByName(main.getClassesTaskName());
        Task processResources = project.getTasks().getByName(
                main.getProcessResourcesTaskName());
        List<String> executed = new ArrayList<>();
        for (Task task : project.getGradle().getTaskGraph().getAllTasks()) {
            if (task.getProject().equals(project)) {
                executed.add(task.getPath());
            }
        }
        Collections.sort(executed);
        result.put("executedTaskPaths", executed);
        result.put("compileJavaTaskPath", compileJava.getPath());
        result.put("processResourcesTaskPath", processResources.getPath());
        result.put("classesTaskPath", classes.getPath());
        result.put("exportTaskPath", project.getTasks()
                .getByName(exportTaskName).getPath());
        result.put("compileJavaActionCount", compileJava.getActions().size());
        result.put("processResourcesActionCount",
                processResources.getActions().size());
        result.put("classesActionCount", classes.getActions().size());
        Path classOutput = compileJava.getDestinationDirectory()
                .get().getAsFile().toPath().toAbsolutePath().normalize();
        List<String> overlapping = new ArrayList<>();
        for (Task task : project.getTasks()) {
            for (File output : task.getOutputs().getFiles().getFiles()) {
                Path candidate = output.toPath().toAbsolutePath().normalize();
                if (candidate.equals(classOutput)
                        || candidate.startsWith(classOutput)
                        || classOutput.startsWith(candidate)) {
                    overlapping.add(task.getPath());
                    break;
                }
            }
        }
        Collections.sort(overlapping);
        result.put("classOutputOverlappingTaskPaths", overlapping);
        return result;
    }

    private static Map<String, Object> sourceSet(SourceSet sourceSet)
            throws IOException {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("name", sourceSet.getName());
        result.put("javaSourceDirectories", orderedFiles(
                sourceSet.getJava().getSrcDirs()));
        result.put("javaIncludes", sorted(
                sourceSet.getJava().getIncludes()));
        result.put("javaExcludes", sorted(
                sourceSet.getJava().getExcludes()));
        result.put("resourceDirectories", orderedFiles(
                sourceSet.getResources().getSrcDirs()));
        result.put("resourceIncludes", sorted(
                sourceSet.getResources().getIncludes()));
        result.put("resourceExcludes", sorted(
                sourceSet.getResources().getExcludes()));
        result.put("classesDirectories", orderedFiles(
                sourceSet.getOutput().getClassesDirs().getFiles()));
        result.put("resourcesDirectory", nullableFile(
                sourceSet.getOutput().getResourcesDir()));
        result.put("compileClasspath", orderedFiles(
                sourceSet.getCompileClasspath().getFiles()));
        result.put("runtimeClasspath", orderedFiles(
                sourceSet.getRuntimeClasspath().getFiles()));
        return result;
    }

    private static Map<String, Object> compileTask(JavaCompile task)
            throws Exception {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("taskPath", task.getPath());
        result.put("sourceCompatibility", task.getSourceCompatibility());
        result.put("targetCompatibility", task.getTargetCompatibility());
        result.put("sourceFiles", orderedFiles(task.getSource().getFiles()));
        result.put("classpath", orderedFiles(task.getClasspath().getFiles()));
        result.put("destinationDirectory", canonical(
                task.getDestinationDirectory().get().getAsFile()));
        result.put("encoding", task.getOptions().getEncoding());
        result.put("debug", task.getOptions().isDebug());
        result.put("fork", task.getOptions().isFork());
        result.put("incremental", task.getOptions().isIncremental());
        result.put("compilerArgumentProviderCount",
                task.getOptions().getCompilerArgumentProviders().size());
        result.put("compilerArgumentProvidersUnmodeled",
                !task.getOptions().getCompilerArgumentProviders().isEmpty());
        result.put("compilerArgsPrivate", new ArrayList<>(
                task.getOptions().getCompilerArgs()));
        result.put("compilerArgsIdentity", identity(
                task.getOptions().getCompilerArgs()));
        result.put("annotationProcessorPath", orderedFiles(
                task.getOptions().getAnnotationProcessorPath() == null
                        ? Collections.<File>emptySet()
                        : task.getOptions().getAnnotationProcessorPath().getFiles()));
        result.put("generatedSourceOutputDirectory", providerFile(
                task.getOptions().getGeneratedSourceOutputDirectory()));
        result.put("release", task.getOptions().getRelease().getOrNull());
        JavaCompiler compiler = task.getJavaCompiler().getOrNull();
        result.put("compilerJavaHome", compiler == null ? null : canonical(
                compiler.getMetadata().getInstallationPath().getAsFile()));
        result.put("compilerJavaVersion", compiler == null ? null
                : compiler.getMetadata().getLanguageVersion().asInt());
        return result;
    }

    private Map<String, Object> testTask(Test task) throws Exception {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("taskPath", task.getPath());
        result.put("framework", framework(task));
        result.put("testClassesDirectories", orderedFiles(
                task.getTestClassesDirs().getFiles()));
        result.put("classpath", orderedFiles(task.getClasspath().getFiles()));
        result.put("workingDirectory", canonical(task.getWorkingDir()));
        result.put("enableAssertions", task.getEnableAssertions());
        result.put("debug", task.getDebug());
        result.put("failFast", task.getFailFast());
        result.put("dryRun", task.getDryRun().getOrElse(false));
        result.put("scanForTestClasses", task.isScanForTestClasses());
        JavaLauncher launcher = task.getJavaLauncher().getOrNull();
        result.put("javaHome", launcher == null ? null : canonical(
                launcher.getMetadata().getInstallationPath().getAsFile()));
        result.put("javaExecutable", launcher == null ? null : canonical(
                launcher.getExecutablePath().getAsFile()));
        result.put("javaVersion", launcher == null ? null
                : launcher.getMetadata().getLanguageVersion().asInt());
        result.put("javaSelectionSource", "resolved_java_launcher");
        result.put("minHeapSize", task.getMinHeapSize());
        result.put("maxHeapSize", task.getMaxHeapSize());
        result.put("jvmArgumentProviderCount",
                task.getJvmArgumentProviders().size());
        result.put("jvmArgumentProvidersUnmodeled",
                !task.getJvmArgumentProviders().isEmpty());
        result.put("bootstrapClasspath", orderedFiles(
                task.getBootstrapClasspath().getFiles()));
        result.put("maxParallelForks", task.getMaxParallelForks());
        result.put("forkEvery", task.getForkEvery());
        List<String> jvmArgs = new ArrayList<>(task.getJvmArgs());
        result.put("jvmArgsPrivate", jvmArgs);
        result.put("jvmArgsIdentity", identity(jvmArgs));

        Map<String, String> properties = stringify(task.getSystemProperties());
        result.put("systemPropertiesPrivate", properties);
        result.put("systemPropertyNames", new ArrayList<>(properties.keySet()));
        result.put("systemPropertiesIdentity", identity(properties));

        Map<String, String> environment = stringify(task.getEnvironment());
        Map<String, String> baseline = baselineEnvironments.get(task.getPath());
        if (baseline == null) {
            throw new IllegalStateException(
                    "Test task environment baseline was not captured.");
        }
        Map<String, String> overrides = environmentDifference(
                environment, baseline);
        result.put("environmentOverridesPrivate", overrides);
        result.put("environmentOverrideNames", new ArrayList<>(overrides.keySet()));
        result.put("environmentOverridesIdentity", identity(overrides));

        result.put("includePatterns", sorted(
                task.getFilter().getIncludePatterns()));
        result.put("excludePatterns", sorted(
                task.getFilter().getExcludePatterns()));
        if (task.getOptions() instanceof JUnitPlatformOptions) {
            JUnitPlatformOptions options =
                    (JUnitPlatformOptions) task.getOptions();
            result.put("includeEngines", sorted(options.getIncludeEngines()));
            result.put("excludeEngines", sorted(options.getExcludeEngines()));
            result.put("includeTags", sorted(options.getIncludeTags()));
            result.put("excludeTags", sorted(options.getExcludeTags()));
        } else {
            result.put("includeEngines", Collections.emptyList());
            result.put("excludeEngines", Collections.emptyList());
            result.put("includeTags", Collections.emptyList());
            result.put("excludeTags", Collections.emptyList());
        }
        return result;
    }

    private static String framework(Test task) {
        if (task.getOptions() instanceof JUnitPlatformOptions) {
            return "junit_platform";
        }
        if (task.getOptions() instanceof TestNGOptions) {
            return "testng";
        }
        if (task.getOptions() instanceof JUnitOptions) {
            return "junit4";
        }
        throw boundary(
                "GRADLE_TEST_FRAMEWORK_UNSUPPORTED",
                "The Test task uses an unsupported framework options type.");
    }

    private static BoundaryException boundary(String code, String message) {
        return new BoundaryException(code, message);
    }

    private static String requiredIdentity(
            String name, int minimum, int maximum) {
        String value = System.getProperty(name);
        if (value == null || value.length() < minimum
                || value.length() > maximum
                || !value.matches("[A-Za-z0-9_.-]+")) {
            throw new GradleException(
                    "GRADLE_PROBE_IDENTITY_INVALID: " + name);
        }
        return value;
    }

    private static Map<String, String> environmentDifference(
            Map<String, String> effective,
            Map<String, String> baseline) {
        Set<String> names = new LinkedHashSet<>();
        names.addAll(effective.keySet());
        names.addAll(baseline.keySet());
        List<String> ordered = new ArrayList<>(names);
        Collections.sort(ordered);
        Map<String, String> result = new LinkedHashMap<>();
        for (String name : ordered) {
            if (!Objects.equals(effective.get(name), baseline.get(name))) {
                result.put(name, effective.get(name));
            }
        }
        return result;
    }

    private static Map<String, String> stringify(Map<?, ?> values) {
        List<String> names = new ArrayList<>();
        for (Object name : values.keySet()) {
            names.add(String.valueOf(name));
        }
        Collections.sort(names);
        Map<String, String> result = new LinkedHashMap<>();
        for (String name : names) {
            Object value = values.get(name);
            result.put(name, value == null ? null : String.valueOf(value));
        }
        return result;
    }

    private static List<String> orderedFiles(Collection<File> values)
            throws IOException {
        List<String> result = new ArrayList<>();
        for (File value : values) {
            result.add(canonical(value));
        }
        return result;
    }

    private static List<String> sorted(Collection<String> values) {
        List<String> result = new ArrayList<>(values);
        Collections.sort(result);
        return result;
    }

    private static String nullableFile(File value) throws IOException {
        return value == null ? null : canonical(value);
    }

    private static String providerFile(
            org.gradle.api.file.DirectoryProperty value) throws IOException {
        return value.isPresent() ? canonical(value.get().getAsFile()) : null;
    }

    private static String canonical(File value) throws IOException {
        return value.getCanonicalFile().toPath().toString();
    }

    private static String identity(Object value) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        digest.update(json(value).getBytes(StandardCharsets.UTF_8));
        return "sha256:" + hex(digest.digest());
    }

    private static String hex(byte[] bytes) {
        char[] alphabet = "0123456789abcdef".toCharArray();
        char[] result = new char[bytes.length * 2];
        for (int index = 0; index < bytes.length; index++) {
            int value = bytes[index] & 0xff;
            result[index * 2] = alphabet[value >>> 4];
            result[index * 2 + 1] = alphabet[value & 0x0f];
        }
        return new String(result);
    }

    private static String json(Object value) {
        if (value == null) {
            return "null";
        }
        if (value instanceof Boolean || value instanceof Number) {
            return String.valueOf(value);
        }
        if (value instanceof Map) {
            StringBuilder out = new StringBuilder("{");
            boolean first = true;
            for (Object raw : ((Map<?, ?>) value).entrySet()) {
                Map.Entry<?, ?> entry = (Map.Entry<?, ?>) raw;
                if (!first) {
                    out.append(',');
                }
                first = false;
                out.append(json(String.valueOf(entry.getKey())))
                        .append(':').append(json(entry.getValue()));
            }
            return out.append('}').toString();
        }
        if (value instanceof Iterable) {
            StringBuilder out = new StringBuilder("[");
            boolean first = true;
            for (Object item : (Iterable<?>) value) {
                if (!first) {
                    out.append(',');
                }
                first = false;
                out.append(json(item));
            }
            return out.append(']').toString();
        }
        String text = String.valueOf(value);
        StringBuilder out = new StringBuilder(text.length() + 2).append('"');
        for (int index = 0; index < text.length(); index++) {
            char current = text.charAt(index);
            switch (current) {
                case '"': out.append("\\\""); break;
                case '\\': out.append("\\\\"); break;
                case '\b': out.append("\\b"); break;
                case '\f': out.append("\\f"); break;
                case '\n': out.append("\\n"); break;
                case '\r': out.append("\\r"); break;
                case '\t': out.append("\\t"); break;
                default:
                    if (current < 0x20) {
                        out.append(String.format("\\u%04x", (int) current));
                    } else {
                        out.append(current);
                    }
            }
        }
        return out.append('"').toString();
    }

    private static void restrict(Path path) {
        try {
            Files.setPosixFilePermissions(
                    path,
                    EnumSet.of(
                            PosixFilePermission.OWNER_READ,
                            PosixFilePermission.OWNER_WRITE));
        } catch (Exception ignored) {
            // Windows ACL inheritance is verified by the product integration.
        }
    }
}
