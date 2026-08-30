package net.jolink.runtime.jdt;

import java.io.BufferedReader;
import java.io.File;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.stream.Stream;

import org.eclipse.core.resources.ICommand;
import org.eclipse.core.resources.IContainer;
import org.eclipse.core.resources.IFile;
import org.eclipse.core.resources.IFolder;
import org.eclipse.core.resources.IMarker;
import org.eclipse.core.resources.IncrementalProjectBuilder;
import org.eclipse.core.resources.IProject;
import org.eclipse.core.resources.IProjectDescription;
import org.eclipse.core.resources.IResource;
import org.eclipse.core.resources.IWorkspace;
import org.eclipse.core.resources.IWorkspaceDescription;
import org.eclipse.core.resources.ResourcesPlugin;
import org.eclipse.core.runtime.CoreException;
import org.eclipse.core.runtime.IPath;
import org.eclipse.core.runtime.NullProgressMonitor;
import org.eclipse.core.runtime.OperationCanceledException;
import org.eclipse.core.runtime.Platform;
import org.eclipse.equinox.app.IApplication;
import org.eclipse.equinox.app.IApplicationContext;
import org.eclipse.jdt.core.IClasspathEntry;
import org.eclipse.jdt.core.IClasspathAttribute;
import org.eclipse.jdt.core.IJavaProject;
import org.eclipse.jdt.core.JavaCore;

/**
 * Small headless protocol worker used by the product CompileSession and its
 * isolated evidence suites.
 *
 * <p>The original synchronous BUILD/SAVE/STOP protocol remains available for
 * A1-A8. A9 additionally uses identity-bound asynchronous build, status,
 * cancellation, barrier, metrics, and GC commands. Stdout contains JSON
 * protocol frames only. Diagnostics belong on stderr.</p>
 */
public final class WorkerApplication implements IApplication {
    private static final String PROJECT_NAME = "plain-fixture";
    private static final int MAX_ERROR_DIAGNOSTICS = 128;
    private static final int MAX_OTHER_DIAGNOSTICS = 32;
    private static final String DIAGNOSTIC_SELECTION_POLICY =
            "errors_first_then_warnings_then_info";
    private static final char[] HEX =
            "0123456789abcdef".toCharArray();

    private final NullProgressMonitor monitor = new NullProgressMonitor();
    private PrintWriter protocol;
    private IWorkspace workspace;
    private IProject project;
    private IJavaProject javaProject;
    private boolean instrumentationEnabled;
    private boolean projectReopened;
    private String requestedSourceEncoding;
    private String canonicalSourceEncoding;
    private String effectiveSourceEncoding;
    private boolean sourceEncodingVerified;
    private boolean testModelConfigured;
    private boolean aptEnabled;
    private int aptFactoryPathRequestedCount;
    private int aptFactoryPathEffectiveCount;
    private String aptFactoryPathRequestedIdentity;
    private String aptFactoryPathEffectiveIdentity;
    private boolean aptFactoryPathVerified;
    private int aptUnexpectedEnabledContainerCount;
    private String aptUnexpectedEnabledContainerIdentity;
    private String aptGeneratedSourceRequested;
    private String aptGeneratedSourceEffective;
    private boolean aptGeneratedSourceVerified;
    private ActiveBuild activeBuild;
    private String lastTerminalRequestId;
    private String lastTerminalBuildGenerationId;
    private String lastTerminalStatus;
    private long protocolSequence;
    private Set<String> previousSourceUnits = new LinkedHashSet<>();
    private final Set<String> pendingDeletedSourceUnits =
            new LinkedHashSet<>();

    private static final class ActiveBuild {
        final String requestId;
        final String buildGenerationId;
        final String operationKind;
        final NullProgressMonitor monitor = new NullProgressMonitor();
        Thread thread;
        boolean cancelAccepted;
        boolean terminalEmitted;

        ActiveBuild(String requestId, String buildGenerationId, String operationKind) {
            this.requestId = requestId;
            this.buildGenerationId = buildGenerationId;
            this.operationKind = operationKind;
        }
    }

    private static final class ProblemDiagnostic
            implements Comparable<ProblemDiagnostic> {
        final String resource;
        final int line;
        final int severity;
        final int characterStart;
        final int characterEnd;
        final String message;

        ProblemDiagnostic(IMarker marker) throws CoreException {
            this.resource = marker.getResource().getProjectRelativePath().toString();
            this.line = marker.getAttribute(IMarker.LINE_NUMBER, -1);
            this.severity = marker.getAttribute(IMarker.SEVERITY, -1);
            this.characterStart = marker.getAttribute(IMarker.CHAR_START, -1);
            this.characterEnd = marker.getAttribute(IMarker.CHAR_END, -1);
            this.message = marker.getAttribute(IMarker.MESSAGE, "");
        }

        @Override
        public int compareTo(ProblemDiagnostic other) {
            int compared = resource.compareTo(other.resource);
            if (compared != 0) {
                return compared;
            }
            compared = Integer.compare(line, other.line);
            if (compared != 0) {
                return compared;
            }
            compared = Integer.compare(characterStart, other.characterStart);
            if (compared != 0) {
                return compared;
            }
            return message.compareTo(other.message);
        }

        String compact() {
            return resource + ":" + line + ":" + severity + ":" + message;
        }

        String detailJson() {
            return "{\"resource\":" + json(resource)
                    + ",\"line\":" + line
                    + ",\"severity\":" + severity
                    + ",\"severity_name\":" + json(severityName(severity))
                    + ",\"character_start\":" + characterStart
                    + ",\"character_end\":" + characterEnd
                    + ",\"message\":" + json(message) + "}";
        }
    }

    @Override
    public Object start(IApplicationContext context) throws Exception {
        protocol = new PrintWriter(
                new java.io.OutputStreamWriter(System.out, StandardCharsets.UTF_8),
                true);
        Map<String, String> arguments = parseArguments(context);
        String systemLibrariesFile = arguments.get("system-libraries");
        if (isBlank(systemLibrariesFile)) {
            emitError("MISSING_SYSTEM_LIBRARIES", "Missing --system-libraries.");
            return Integer.valueOf(2);
        }
        String sourceEncoding = arguments.get("source-encoding");
        if (isBlank(sourceEncoding)) {
            emitError("MISSING_SOURCE_ENCODING", "Missing --source-encoding.");
            return Integer.valueOf(2);
        }
        try {
            Charset.forName(sourceEncoding);
        } catch (IllegalArgumentException exception) {
            emitError("INVALID_SOURCE_ENCODING", "Unsupported source encoding.");
            return Integer.valueOf(2);
        }
        String sourceLevel = arguments.getOrDefault("source-level", "8");
        if (!"8".equals(sourceLevel) && !"11".equals(sourceLevel)) {
            emitError("INVALID_SOURCE_LEVEL", "Unsupported source level.");
            return Integer.valueOf(2);
        }
        boolean methodParameters = "true".equalsIgnoreCase(
                arguments.getOrDefault("parameters", "false"));
        instrumentationEnabled = !"disabled".equals(
                arguments.getOrDefault("instrumentation", "enabled"));
        BuildObservation.setEnabled(instrumentationEnabled);

        try {
            String aptProcessorsFile = arguments.get("apt-processors-file");
            String testClasspathFile = arguments.get("test-classpath-file");
            initialize(
                    Paths.get(systemLibrariesFile),
                    sourceEncoding,
                    sourceLevel,
                    methodParameters,
                    aptProcessorsFile == null
                            ? null : Paths.get(aptProcessorsFile),
                    testClasspathFile == null
                            ? null : Paths.get(testClasspathFile));
            emitReady();
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
                for (String line; (line = reader.readLine()) != null;) {
                    if (!handle(line)) {
                        return EXIT_OK;
                    }
                }
            }
            saveWorkspace();
            return EXIT_OK;
        } catch (Exception exception) {
            exception.printStackTrace(System.err);
            emitError("WORKER_FAILED", exception.getClass().getSimpleName());
            return Integer.valueOf(1);
        }
    }

    @Override
    public void stop() {
        try {
            if (workspace != null) {
                saveWorkspace();
            }
        } catch (CoreException exception) {
            exception.printStackTrace(System.err);
        }
    }

    private static Map<String, String> parseArguments(IApplicationContext context) {
        Object raw = context.getArguments().get(IApplicationContext.APPLICATION_ARGS);
        String[] values = raw instanceof String[] ? (String[]) raw : new String[0];
        Map<String, String> result = new LinkedHashMap<>();
        for (int index = 0; index < values.length; index++) {
            String value = values[index];
            if (value.startsWith("--") && index + 1 < values.length) {
                result.put(value.substring(2), values[++index]);
            }
        }
        return result;
    }

    private void initialize(
            Path systemLibrariesFile,
            String sourceEncoding,
            String sourceLevel,
            boolean methodParameters,
            Path aptProcessorsFile,
            Path testClasspathFile) throws Exception {
        workspace = ResourcesPlugin.getWorkspace();
        IWorkspaceDescription workspaceDescription = workspace.getDescription();
        workspaceDescription.setAutoBuilding(false);
        workspace.setDescription(workspaceDescription);

        project = workspace.getRoot().getProject(PROJECT_NAME);
        projectReopened = project.exists();
        if (!project.exists()) {
            project.create(monitor);
        }
        if (!project.isOpen()) {
            project.open(monitor);
        }

        IProjectDescription projectDescription = project.getDescription();
        boolean descriptionChanged = false;
        if (!Arrays.equals(
                projectDescription.getNatureIds(),
                new String[] { JavaCore.NATURE_ID })) {
            projectDescription.setNatureIds(new String[] { JavaCore.NATURE_ID });
            descriptionChanged = true;
        }
        ICommand[] existingBuildSpec = projectDescription.getBuildSpec();
        if (existingBuildSpec.length != 1
                || !JavaCore.BUILDER_ID.equals(
                        existingBuildSpec[0].getBuilderName())) {
            ICommand builder = projectDescription.newCommand();
            builder.setBuilderName(JavaCore.BUILDER_ID);
            projectDescription.setBuildSpec(new ICommand[] { builder });
            descriptionChanged = true;
        }
        if (descriptionChanged) {
            project.setDescription(projectDescription, monitor);
        }

        IFolder source = ensureFolder(project.getFolder("src"));
        requestedSourceEncoding = sourceEncoding;
        Charset requestedCharset = Charset.forName(sourceEncoding);
        canonicalSourceEncoding = requestedCharset.name();
        String inheritedSourceEncoding = source.getDefaultCharset(true);
        if (!requestedCharset.equals(Charset.forName(inheritedSourceEncoding))) {
            source.setDefaultCharset(canonicalSourceEncoding, monitor);
        }
        Charset effectiveCharset = Charset.forName(
                source.getDefaultCharset(true));
        effectiveSourceEncoding = effectiveCharset.name();
        sourceEncodingVerified = requestedCharset.equals(effectiveCharset);
        if (!sourceEncodingVerified) {
            throw new IOException(
                    "Eclipse Resources did not apply the requested source encoding.");
        }
        IFolder output = ensureFolder(project.getFolder("bin"));
        javaProject = JavaCore.create(project);
        if (!output.getFullPath().equals(javaProject.getOutputLocation())) {
            javaProject.setOutputLocation(output.getFullPath(), monitor);
        }

        List<IClasspathEntry> classpath = new ArrayList<>();
        classpath.add(JavaCore.newSourceEntry(
                source.getFullPath(),
                new IPath[0],
                new IPath[0],
                output.getFullPath(),
                new IClasspathAttribute[0]));
        for (String line : Files.readAllLines(
                systemLibrariesFile, StandardCharsets.UTF_8)) {
            String value = line.trim();
            if (!value.isEmpty()) {
                Path entry = Paths.get(value);
                if (!Files.exists(entry)) {
                    throw new IOException("System library entry is unavailable.");
                }
                classpath.add(JavaCore.newLibraryEntry(
                        org.eclipse.core.runtime.Path.fromOSString(
                                entry.toAbsolutePath().normalize().toString()),
                        null,
                        null));
            }
        }
        if (testClasspathFile != null) {
            if (!Files.isRegularFile(testClasspathFile)) {
                throw new IOException("Test classpath file is unavailable.");
            }
            IFolder testSource = ensureFolder(project.getFolder("test-src"));
            IFolder testOutput = ensureFolder(project.getFolder("test-bin"));
            if (!requestedCharset.equals(Charset.forName(
                    testSource.getDefaultCharset(true)))) {
                testSource.setDefaultCharset(canonicalSourceEncoding, monitor);
            }
            if (!requestedCharset.equals(Charset.forName(
                    testSource.getDefaultCharset(true)))) {
                throw new IOException(
                        "Eclipse Resources did not apply test source encoding.");
            }
            IClasspathAttribute[] testAttributes = new IClasspathAttribute[] {
                JavaCore.newClasspathAttribute(
                        IClasspathAttribute.TEST,
                        Boolean.TRUE.toString())
            };
            classpath.add(JavaCore.newSourceEntry(
                    testSource.getFullPath(),
                    new IPath[0],
                    new IPath[0],
                    testOutput.getFullPath(),
                    testAttributes));
            for (String line : Files.readAllLines(
                    testClasspathFile, StandardCharsets.UTF_8)) {
                String value = line.trim();
                if (value.isEmpty()) {
                    continue;
                }
                Path entry = Paths.get(value);
                if (!Files.exists(entry)) {
                    throw new IOException(
                            "Test classpath entry is unavailable.");
                }
                classpath.add(JavaCore.newLibraryEntry(
                        org.eclipse.core.runtime.Path.fromOSString(
                                entry.toAbsolutePath().normalize().toString()),
                        null,
                        null,
                        new org.eclipse.jdt.core.IAccessRule[0],
                        testAttributes,
                        false));
            }
            testModelConfigured = true;
        }
        if (classpath.size() == 1) {
            throw new IOException("System library snapshot is empty.");
        }
        IClasspathEntry[] desiredClasspath =
                classpath.toArray(new IClasspathEntry[0]);
        if (!Arrays.equals(javaProject.getRawClasspath(), desiredClasspath)) {
            javaProject.setRawClasspath(desiredClasspath, monitor);
        }

        Map<String, String> options = new LinkedHashMap<>(
                javaProject.getOptions(false));
        String compliance = "11".equals(sourceLevel)
                ? JavaCore.VERSION_11 : JavaCore.VERSION_1_8;
        JavaCore.setComplianceOptions(compliance, options);
        options.put(JavaCore.COMPILER_SOURCE, compliance);
        options.put(JavaCore.COMPILER_COMPLIANCE, compliance);
        options.put(JavaCore.COMPILER_CODEGEN_TARGET_PLATFORM, compliance);
        options.put(
                JavaCore.COMPILER_CODEGEN_METHOD_PARAMETERS_ATTR,
                methodParameters ? JavaCore.GENERATE : JavaCore.DO_NOT_GENERATE);
        options.put(JavaCore.COMPILER_PB_ENABLE_PREVIEW_FEATURES, JavaCore.DISABLED);
        if (!javaProject.getOptions(false).equals(options)) {
            javaProject.setOptions(options);
        }
        if (aptProcessorsFile != null) {
            configureApt(aptProcessorsFile);
        }
        project.refreshLocal(IResource.DEPTH_INFINITE, monitor);
        previousSourceUnits = sourceUnits();
        pendingDeletedSourceUnits.clear();
    }

    private Set<String> sourceUnits() throws IOException {
        Set<String> result = new LinkedHashSet<>();
        collectSourceUnits("src", result);
        collectSourceUnits("test-src", result);
        return result;
    }

    private void collectSourceUnits(String folderName, Set<String> result)
            throws IOException {
        IFolder folder = project.getFolder(folderName);
        if (!folder.exists() || folder.getLocation() == null) {
            return;
        }
        Path root = folder.getLocation().toFile().toPath();
        try (Stream<Path> paths = Files.walk(root)) {
            paths.filter(path -> Files.isRegularFile(path))
                    .filter(path -> path.getFileName().toString().endsWith(".java"))
                    .forEach(path -> result.add(
                            folderName + "/" + root.relativize(path)
                                    .toString().replace(File.separatorChar, '/')));
        }
    }

    private void configureApt(Path processorsFile) throws Exception {
        if (!Files.isRegularFile(processorsFile)) {
            throw new IOException("APT processor path file is unavailable.");
        }
        List<File> processors = new ArrayList<>();
        for (String line : Files.readAllLines(
                processorsFile, StandardCharsets.UTF_8)) {
            String value = line.trim();
            if (value.isEmpty()) {
                continue;
            }
            Path path = Paths.get(value).toAbsolutePath().normalize();
            if (!Files.isRegularFile(path)) {
                throw new IOException("APT processor path entry is unavailable.");
            }
            processors.add(path.toFile());
        }
        if (processors.isEmpty()) {
            throw new IOException("APT processor path is empty.");
        }

        org.osgi.framework.Bundle aptBundle = Platform.getBundle(
                "org.eclipse.jdt.apt.core");
        if (aptBundle == null) {
            throw new IOException("Eclipse APT core bundle is unavailable.");
        }
        Class<?> aptConfig = aptBundle.loadClass(
                "org.eclipse.jdt.apt.core.util.AptConfig");
        Class<?> factoryPathType = aptBundle.loadClass(
                "org.eclipse.jdt.apt.core.util.IFactoryPath");
        aptConfig.getMethod("initialize").invoke(null);
        aptGeneratedSourceRequested = ".apt_generated";
        aptConfig.getMethod(
                "setGenSrcDir", IJavaProject.class, String.class).invoke(
                        null, javaProject, aptGeneratedSourceRequested);
        aptConfig.getMethod(
                "setProcessDuringReconcile",
                IJavaProject.class,
                boolean.class).invoke(null, javaProject, false);
        Object factoryPath = aptConfig.getMethod(
                "getDefaultFactoryPath", IJavaProject.class).invoke(
                        null, javaProject);
        for (int index = processors.size() - 1; index >= 0; index--) {
            factoryPathType.getMethod("addExternalJar", File.class).invoke(
                    factoryPath, processors.get(index));
        }
        aptConfig.getMethod(
                "setFactoryPath", IJavaProject.class, factoryPathType).invoke(
                        null, javaProject, factoryPath);
        aptConfig.getMethod(
                "setEnabled", IJavaProject.class, boolean.class).invoke(
                        null, javaProject, true);
        aptEnabled = (Boolean) aptConfig.getMethod(
                "isEnabled", IJavaProject.class).invoke(null, javaProject);
        aptGeneratedSourceEffective = (String) aptConfig.getMethod(
                "getGenSrcDir", IJavaProject.class).invoke(null, javaProject);
        aptGeneratedSourceVerified = aptGeneratedSourceRequested.equals(
                aptGeneratedSourceEffective);

        List<String> requestedPaths = new ArrayList<>();
        for (File processor : processors) {
            requestedPaths.add(
                    processor.toPath().toRealPath().toString());
        }
        Object effectiveFactoryPath = aptConfig.getMethod(
                "getFactoryPath", IJavaProject.class).invoke(null, javaProject);
        @SuppressWarnings("unchecked")
        Map<Object, Object> enabledContainers = (Map<Object, Object>)
                effectiveFactoryPath.getClass().getMethod(
                        "getEnabledContainers").invoke(effectiveFactoryPath);
        List<String> effectivePaths = new ArrayList<>();
        List<String> unexpectedContainers = new ArrayList<>();
        for (Object container : enabledContainers.keySet()) {
            Object type = container.getClass().getMethod("getType").invoke(
                    container);
            String id = (String) container.getClass().getMethod("getId").invoke(
                    container);
            if (!"EXTJAR".equals(String.valueOf(type))) {
                unexpectedContainers.add(String.valueOf(type) + ":" + id);
                continue;
            }
            effectivePaths.add(Paths.get(id).toRealPath().toString());
        }
        Collections.sort(unexpectedContainers);
        aptFactoryPathRequestedCount = requestedPaths.size();
        aptFactoryPathEffectiveCount = effectivePaths.size();
        aptFactoryPathRequestedIdentity = stringListIdentity(requestedPaths);
        aptFactoryPathEffectiveIdentity = stringListIdentity(effectivePaths);
        aptUnexpectedEnabledContainerCount = unexpectedContainers.size();
        aptUnexpectedEnabledContainerIdentity = stringListIdentity(
                unexpectedContainers);
        aptFactoryPathVerified = requestedPaths.equals(effectivePaths)
                && unexpectedContainers.isEmpty();
        if (!aptEnabled) {
            throw new IOException("Eclipse APT did not become enabled.");
        }
        if (!aptGeneratedSourceVerified) {
            throw new IOException(
                    "Eclipse APT generated-source directory was not applied.");
        }
        if (!aptFactoryPathVerified) {
            throw new IOException("Eclipse APT factory path was not applied.");
        }
    }

    private static String stringListIdentity(List<String> values)
            throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        for (String value : values) {
            byte[] encoded = value.getBytes(StandardCharsets.UTF_8);
            digest.update((byte) (encoded.length >>> 24));
            digest.update((byte) (encoded.length >>> 16));
            digest.update((byte) (encoded.length >>> 8));
            digest.update((byte) encoded.length);
            digest.update(encoded);
        }
        byte[] bytes = digest.digest();
        StringBuilder result = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            result.append(String.format("%02x", value & 0xff));
        }
        return result.toString();
    }

    private static IFolder ensureFolder(IFolder folder) throws CoreException {
        if (!folder.exists()) {
            IContainer parent = folder.getParent();
            if (parent instanceof IFolder) {
                ensureFolder((IFolder) parent);
            }
            folder.create(true, true, new NullProgressMonitor());
        }
        return folder;
    }

    private boolean handle(String line) throws Exception {
        if (line.equals("METRICS")) {
            emit("{\"ok\":true,\"status\":\"metrics\",\"metrics\":"
                    + WorkerMetrics.snapshotJson(false) + "}");
            return true;
        }
        if (line.equals("GC")) {
            System.gc();
            emit("{\"ok\":true,\"status\":\"gc_requested\",\"metrics\":"
                    + WorkerMetrics.snapshotJson(true) + "}");
            return true;
        }
        if (line.startsWith("BUILD_ASYNC\t")) {
            String[] parts = line.split("\\t", -1);
            if (parts.length == 4) {
                startAsyncBuild(parts[1], parts[2], parts[3]);
            } else {
                emitBuildError(
                        "INVALID_COMMAND",
                        "BUILD_ASYNC requires request/build/kind.",
                        parts.length > 1 ? parts[1] : null,
                        parts.length > 2 ? parts[2] : null);
            }
            return true;
        }
        if (line.startsWith("STATUS\t")) {
            String[] parts = line.split("\\t", -1);
            emitActiveStatus(parts.length == 2 ? parts[1] : "");
            return true;
        }
        if (line.startsWith("CANCEL\t")) {
            String[] parts = line.split("\\t", -1);
            cancelActiveBuild(parts.length == 2 ? parts[1] : "");
            return true;
        }
        if (line.startsWith("BARRIER\t")) {
            handleBarrier(line.split("\\t", -1));
            return true;
        }
        if (line.equals("SAVE")) {
            if (hasActiveBuild()) {
                emitError("ACTIVE_BUILD", "Cannot save while a build is active.");
                return true;
            }
            saveWorkspace();
            emit("{\"ok\":true,\"status\":\"saved\"}");
            return true;
        }
        if (line.equals("STOP")) {
            settleActiveBuildForStop();
            saveWorkspace();
            emit("{\"ok\":true,\"status\":\"stopped\"}");
            return false;
        }
        if (line.startsWith("BUILD\t")) {
            String kind = line.substring("BUILD\t".length());
            if (kind.equals("FULL")) {
                build(IncrementalProjectBuilder.FULL_BUILD, kind,
                        new NullProgressMonitor(), null);
                return true;
            }
            if (kind.equals("INCREMENTAL")) {
                build(IncrementalProjectBuilder.INCREMENTAL_BUILD, kind,
                        new NullProgressMonitor(), null);
                return true;
            }
            if (kind.equals("CLEAN")) {
                build(IncrementalProjectBuilder.CLEAN_BUILD, kind,
                        new NullProgressMonitor(), null);
                return true;
            }
        }
        emitError("INVALID_COMMAND", "Unsupported worker command.");
        return true;
    }

    private void startAsyncBuild(
            String requestId, String buildGenerationId, String operationKind) {
        final int buildKind;
        if ("FULL".equals(operationKind)) {
            buildKind = IncrementalProjectBuilder.FULL_BUILD;
        } else if ("INCREMENTAL".equals(operationKind)) {
            buildKind = IncrementalProjectBuilder.INCREMENTAL_BUILD;
        } else if ("CLEAN".equals(operationKind)) {
            buildKind = IncrementalProjectBuilder.CLEAN_BUILD;
        } else {
            emitBuildError(
                    "INVALID_BUILD_KIND",
                    "Unsupported asynchronous build kind.",
                    requestId,
                    buildGenerationId);
            return;
        }
        synchronized (this) {
            if (activeBuild != null) {
                emitBuildError(
                        "ACTIVE_BUILD",
                        "Only one build may be active.",
                        requestId,
                        buildGenerationId);
                return;
            }
            ActiveBuild build = new ActiveBuild(
                    requestId, buildGenerationId, operationKind);
            activeBuild = build;
            build.thread = new Thread(() -> runAsyncBuild(buildKind, build),
                    "jolink-jdt-build-" + buildGenerationId);
            build.thread.setDaemon(false);
            // A request acknowledgement must precede every asynchronous
            // terminal frame, including an immediately completing no-op.
            emit("{\"ok\":true,\"status\":\"BUILD_ACCEPTED\""
                    + identityFields(requestId, buildGenerationId)
                    + ",\"operation_kind\":" + json(operationKind) + "}");
            build.thread.start();
        }
    }

    private void runAsyncBuild(int buildKind, ActiveBuild build) {
        try {
            build(buildKind, build.operationKind, build.monitor, build);
        } catch (OperationCanceledException exception) {
            emitTerminal(build, "BUILD_CANCELLED", null);
        } catch (Throwable throwable) {
            throwable.printStackTrace(System.err);
            emitTerminal(build, "BUILD_ABORTED", throwable.getClass().getSimpleName());
        } finally {
            synchronized (this) {
                if (activeBuild == build) {
                    activeBuild = null;
                }
                notifyAll();
            }
            BuildObservation.clearBarrier();
        }
    }

    private synchronized boolean hasActiveBuild() {
        return activeBuild != null;
    }

    private synchronized void emitActiveStatus(String buildGenerationId) {
        if (activeBuild == null) {
            emitFinishedOrStale(buildGenerationId);
            return;
        }
        if (!activeBuild.buildGenerationId.equals(buildGenerationId)) {
            emitBuildError(
                    "STALE_BUILD_ID",
                    "The build generation is not active.",
                    null,
                    buildGenerationId);
            return;
        }
        if (activeBuild.terminalEmitted) {
            emitFinishedOrStale(buildGenerationId);
            return;
        }
        emit("{\"ok\":true,\"status\":\"BUILDING\""
                + identityFields(activeBuild.requestId, activeBuild.buildGenerationId)
                + ",\"operation_kind\":" + json(activeBuild.operationKind)
                + ",\"cancel_requested\":" + activeBuild.cancelAccepted
                + ",\"barrier_reached\":"
                + BuildObservation.barrierReached(buildGenerationId) + "}");
    }

    private synchronized void cancelActiveBuild(String buildGenerationId) {
        if (activeBuild == null) {
            emitFinishedOrStale(buildGenerationId);
            return;
        }
        if (!activeBuild.buildGenerationId.equals(buildGenerationId)) {
            emitBuildError(
                    "STALE_BUILD_ID",
                    "The build generation is not active.",
                    null,
                    buildGenerationId);
            return;
        }
        if (activeBuild.terminalEmitted) {
            emit("{\"ok\":false,\"status\":\"ALREADY_FINISHED\""
                    + identityFields(
                            activeBuild.requestId,
                            activeBuild.buildGenerationId)
                    + ",\"terminal_event\":"
                    + json(lastTerminalStatus == null
                            ? "UNKNOWN" : lastTerminalStatus)
                    + "}");
            return;
        }
        activeBuild.cancelAccepted = true;
        activeBuild.monitor.setCanceled(true);
        emit("{\"ok\":true,\"status\":\"CANCEL_REQUESTED\""
                + identityFields(activeBuild.requestId, activeBuild.buildGenerationId)
                + "}");
    }

    private void handleBarrier(String[] parts) {
        if (parts.length != 4) {
            emitBuildError(
                    "INVALID_COMMAND",
                    "BARRIER requires action, request id, and build id.",
                    parts.length > 2 ? parts[2] : null,
                    parts.length > 3 ? parts[3] : null);
            return;
        }
        String requestId = parts[2];
        String buildGenerationId = parts[3];
        if ("ARM".equals(parts[1])) {
            BuildObservation.armBarrier(buildGenerationId);
            emit("{\"ok\":true,\"status\":\"BARRIER_ARMED\""
                    + identityFields(requestId, buildGenerationId) + "}");
        } else if ("RELEASE".equals(parts[1])) {
            // Order the acknowledgement before unblocking the build thread so
            // the asynchronous terminal event cannot overtake this response.
            emit("{\"ok\":true,\"status\":\"BARRIER_RELEASED\""
                    + identityFields(requestId, buildGenerationId) + "}");
            BuildObservation.releaseBarrier(buildGenerationId);
        } else {
            emitBuildError(
                    "INVALID_COMMAND",
                    "Unsupported BARRIER action.",
                    requestId,
                    buildGenerationId);
        }
    }

    private void settleActiveBuildForStop() throws InterruptedException {
        ActiveBuild build;
        synchronized (this) {
            build = activeBuild;
            if (build == null) {
                return;
            }
            build.cancelAccepted = true;
            build.monitor.setCanceled(true);
        }
        BuildObservation.releaseBarrier(build.buildGenerationId);
        build.thread.join(5000L);
        if (build.thread.isAlive()) {
            throw new IllegalStateException("Active build did not settle before STOP.");
        }
    }

    private void build(
            int buildKind,
            String requestedKind,
            NullProgressMonitor buildMonitor,
            ActiveBuild active) throws Exception {
        Map<String, String> before = outputHashes();
        BuildObservation.begin();
        WorkerMetrics.resetPeaks();
        long started = System.nanoTime();
        project.refreshLocal(IResource.DEPTH_INFINITE, buildMonitor);
        Set<String> currentSourceUnits = sourceUnits();
        for (String previous : previousSourceUnits) {
            if (!currentSourceUnits.contains(previous)) {
                pendingDeletedSourceUnits.add(previous);
            }
        }
        pendingDeletedSourceUnits.removeAll(currentSourceUnits);
        project.build(buildKind, buildMonitor);
        if (buildMonitor.isCanceled()) {
            throw new OperationCanceledException();
        }
        long elapsedMillis = (System.nanoTime() - started) / 1_000_000L;
        Map<String, String> after = outputHashes();
        BuildObservation.Snapshot observation = BuildObservation.snapshot();

        List<String> changed = new ArrayList<>();
        List<String> deleted = new ArrayList<>();
        for (Map.Entry<String, String> entry : after.entrySet()) {
            if (!entry.getValue().equals(before.get(entry.getKey()))) {
                changed.add(entry.getKey());
            }
        }
        for (String relative : before.keySet()) {
            if (!after.containsKey(relative)) {
                deleted.add(relative);
            }
        }
        Collections.sort(changed);
        Collections.sort(deleted);

        IMarker[] markers = project.findMarkers(
                IMarker.PROBLEM, true, IResource.DEPTH_INFINITE);
        List<ProblemDiagnostic> errorDiagnostics = new ArrayList<>();
        List<ProblemDiagnostic> warningDiagnostics = new ArrayList<>();
        List<ProblemDiagnostic> infoDiagnostics = new ArrayList<>();
        for (IMarker marker : markers) {
            ProblemDiagnostic diagnostic = new ProblemDiagnostic(marker);
            if (diagnostic.severity == IMarker.SEVERITY_ERROR) {
                errorDiagnostics.add(diagnostic);
            } else if (diagnostic.severity == IMarker.SEVERITY_WARNING) {
                warningDiagnostics.add(diagnostic);
            } else {
                infoDiagnostics.add(diagnostic);
            }
        }
        Collections.sort(errorDiagnostics);
        Collections.sort(warningDiagnostics);
        Collections.sort(infoDiagnostics);

        List<ProblemDiagnostic> selectedDiagnostics = new ArrayList<>();
        selectedDiagnostics.addAll(errorDiagnostics.subList(
                0, Math.min(errorDiagnostics.size(), MAX_ERROR_DIAGNOSTICS)));
        int remainingOther = MAX_OTHER_DIAGNOSTICS;
        int returnedWarningCount = Math.min(warningDiagnostics.size(), remainingOther);
        selectedDiagnostics.addAll(warningDiagnostics.subList(0, returnedWarningCount));
        remainingOther -= returnedWarningCount;
        int returnedInfoCount = Math.min(infoDiagnostics.size(), remainingOther);
        selectedDiagnostics.addAll(infoDiagnostics.subList(0, returnedInfoCount));

        List<String> diagnostics = new ArrayList<>();
        List<String> diagnosticDetails = new ArrayList<>();
        for (ProblemDiagnostic diagnostic : selectedDiagnostics) {
            diagnostics.add(diagnostic.compact());
            diagnosticDetails.add(diagnostic.detailJson());
        }
        int errorCount = errorDiagnostics.size();
        int warningCount = warningDiagnostics.size();
        int infoCount = infoDiagnostics.size();
        int testErrorCount = diagnosticCount(errorDiagnostics, true);
        int mainErrorCount = errorCount - testErrorCount;
        int testWarningCount = diagnosticCount(warningDiagnostics, true);
        int mainWarningCount = warningCount - testWarningCount;
        int returnedErrorCount = Math.min(errorCount, MAX_ERROR_DIAGNOSTICS);

        String actualBuildKind = observation.actualBuildKind();
        boolean compileOperation = !"CLEAN".equals(requestedKind);
        boolean compileOk = errorCount == 0;
        boolean compilerOutputEligible = compileOperation && compileOk;
        List<String> deletedSourceUnits = new ArrayList<>(
                pendingDeletedSourceUnits);
        Collections.sort(deletedSourceUnits);
        StringBuilder result = new StringBuilder();
        result.append("{\"ok\":").append(errorCount == 0)
                .append(",\"status\":")
                .append(json(active == null ? "build_finished" : "BUILD_COMPLETED"))
                .append(",\"operation_kind\":").append(json(requestedKind))
                .append(",\"operation_ok\":true")
                .append(",\"compile_ok\":")
                .append(compileOperation ? Boolean.toString(compileOk) : "null")
                .append(",\"terminal_status\":")
                .append(json(compileOk || !compileOperation
                        ? "SUCCEEDED" : "FAILED_COMPILE"))
                .append(",\"requested_build_kind\":").append(json(requestedKind))
                .append(",\"actual_build_kind\":")
                .append(actualBuildKind == null ? "null" : json(actualBuildKind))
                .append(",\"build_outcome\":")
                .append(json(observation.buildOutcome()))
                .append(",\"project_build_returned\":true")
                .append(",\"compilation_observation\":{")
                .append("\"status\":")
                .append(json(observation.enabled ? "enabled" : "disabled"))
                .append(",\"callbacks_seen\":")
                .append(observation.callbacksSeen())
                .append(",\"batch_seen\":").append(observation.batchSeen)
                .append(",\"incremental_compile_seen\":")
                .append(observation.incrementalSeen)
                .append(",\"build_finished\":")
                .append(observation.buildFinished)
                .append(",\"compiled_source_units\":")
                .append(jsonArray(observation.compiledUnits))
                .append(",\"deleted_source_units\":")
                .append(jsonArray(deletedSourceUnits))
                .append("}")
                .append(",\"resource_delta\":{")
                .append("\"status\":\"unavailable\",")
                .append("\"reason\":\"resource_delta_instrumentation_not_implemented\"}")
                .append(",\"observer_build_finished\":")
                .append(observation.buildFinished)
                .append(",\"compiled_source_units\":")
                .append(jsonArray(observation.compiledUnits))
                .append(",\"deleted_source_units\":")
                .append(jsonArray(deletedSourceUnits))
                .append(",\"elapsed_ms\":").append(elapsedMillis)
                .append(",\"error_count\":").append(errorCount)
                .append(",\"warning_count\":").append(warningCount)
                .append(",\"main_error_count\":").append(mainErrorCount)
                .append(",\"test_error_count\":").append(testErrorCount)
                .append(",\"main_warning_count\":").append(mainWarningCount)
                .append(",\"test_warning_count\":").append(testWarningCount)
                .append(",\"main_compile_ok\":")
                .append(mainErrorCount == 0)
                .append(",\"test_compile_ok\":")
                .append(testErrorCount == 0)
                .append(",\"info_count\":").append(infoCount)
                .append(",\"returned_error_count\":")
                .append(returnedErrorCount)
                .append(",\"returned_warning_count\":")
                .append(returnedWarningCount)
                .append(",\"returned_info_count\":")
                .append(returnedInfoCount)
                .append(",\"diagnostic_selection_policy\":")
                .append(json(DIAGNOSTIC_SELECTION_POLICY))
                .append(",\"compiler_output_eligible\":")
                .append(compilerOutputEligible)
                // The Worker can only report compiler eligibility. Oracle
                // validation and publication commit belong to the Runner.
                .append(",\"generation_publishable\":false")
                .append(",\"class_count\":").append(after.size())
                .append(",\"changed_classes\":").append(jsonArray(changed))
                .append(",\"publishable_changed_classes\":[]")
                .append(",\"deleted_classes\":").append(jsonArray(deleted))
                .append(",\"diagnostics\":").append(jsonArray(diagnostics))
                .append(",\"diagnostic_details\":")
                .append(jsonObjectsArray(diagnosticDetails))
                .append(",\"diagnostics_truncated\":")
                .append(selectedDiagnostics.size() < markers.length)
                .append(",\"metrics\":")
                .append(WorkerMetrics.snapshotJson(false));
        if (active != null) {
            result.append(identityFields(
                    active.requestId, active.buildGenerationId))
                    .append(",\"terminal_record_source\":\"worker\"");
        }
        result
                .append("}");
        previousSourceUnits = currentSourceUnits;
        if (compileOk) {
            pendingDeletedSourceUnits.clear();
        }
        if (active == null) {
            emit(result.toString());
        } else {
            emitTerminal(active, result.toString());
        }
    }

    private static int diagnosticCount(
            List<ProblemDiagnostic> diagnostics,
            boolean test) {
        int count = 0;
        for (ProblemDiagnostic diagnostic : diagnostics) {
            boolean testResource = diagnostic.resource.startsWith("test-src/");
            if (testResource == test) {
                count++;
            }
        }
        return count;
    }

    private synchronized void emitTerminal(
            ActiveBuild build, String status, String errorType) {
        String frame = "{\"ok\":false,\"status\":" + json(status)
                + identityFields(build.requestId, build.buildGenerationId)
                + ",\"operation_kind\":" + json(build.operationKind)
                + ",\"operation_ok\":false,\"compile_ok\":null"
                + ",\"compiler_output_eligible\":false"
                + ",\"generation_publishable\":false"
                + ",\"publishable_changed_classes\":[]"
                + ",\"terminal_status\":"
                + json("BUILD_CANCELLED".equals(status) ? "CANCELLED" : "ABORTED")
                + ",\"terminal_record_source\":\"worker\""
                + (errorType == null ? "" : ",\"error_type\":" + json(errorType))
                + "}";
        emitTerminal(build, frame);
    }

    private synchronized void emitTerminal(ActiveBuild build, String frame) {
        if (build.terminalEmitted) {
            return;
        }
        build.terminalEmitted = true;
        lastTerminalRequestId = build.requestId;
        lastTerminalBuildGenerationId = build.buildGenerationId;
        int statusStart = frame.indexOf("\"status\":\"");
        if (statusStart >= 0) {
            statusStart += "\"status\":\"".length();
            int statusEnd = frame.indexOf('"', statusStart);
            lastTerminalStatus = statusEnd > statusStart
                    ? frame.substring(statusStart, statusEnd) : "UNKNOWN";
        }
        emit(frame);
    }

    private void emitFinishedOrStale(String buildGenerationId) {
        if (buildGenerationId != null
                && buildGenerationId.equals(lastTerminalBuildGenerationId)) {
            emit("{\"ok\":false,\"status\":\"ALREADY_FINISHED\""
                    + identityFields(
                            lastTerminalRequestId,
                            lastTerminalBuildGenerationId)
                    + ",\"terminal_event\":"
                    + json(lastTerminalStatus == null
                            ? "UNKNOWN" : lastTerminalStatus)
                    + "}");
        } else {
            emitBuildError(
                    "STALE_BUILD_ID",
                    "The build generation is not active.",
                    null,
                    buildGenerationId);
        }
    }

    private synchronized String identityFields(
            String requestId, String buildGenerationId) {
        return ",\"request_id\":" + jsonNullable(requestId)
                + ",\"build_generation_id\":" + jsonNullable(buildGenerationId)
                + ",\"protocol_sequence\":" + (++protocolSequence);
    }

    private Map<String, String> outputHashes() throws Exception {
        Map<String, String> hashes = new TreeMap<>();
        IFolder output = project.getFolder("bin");
        if (!output.exists()) {
            return hashes;
        }
        output.accept(resource -> {
            if (resource.getType() == IResource.FILE
                    && resource.getName().endsWith(".class")) {
                IFile file = (IFile) resource;
                IPath location = file.getLocation();
                if (location != null) {
                    try {
                        Path path = Paths.get(location.toOSString());
                        String relative = location
                                .makeRelativeTo(output.getLocation())
                                .toString();
                        hashes.put(relative, sha256(path));
                    } catch (Exception exception) {
                        throw new CoreException(new org.eclipse.core.runtime.Status(
                                org.eclipse.core.runtime.IStatus.ERROR,
                                WorkerApplication.class,
                                "Unable to hash class output.",
                                exception));
                    }
                }
            }
            return true;
        });
        return hashes;
    }

    private static String sha256(Path path) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (java.io.InputStream stream = Files.newInputStream(path)) {
            byte[] buffer = new byte[8192];
            for (int read; (read = stream.read(buffer)) >= 0;) {
                digest.update(buffer, 0, read);
            }
        }
        return hex(digest.digest());
    }

    private static boolean isBlank(String value) {
        if (value == null || value.isEmpty()) {
            return true;
        }
        for (int index = 0; index < value.length(); index++) {
            if (!Character.isWhitespace(value.charAt(index))) {
                return false;
            }
        }
        return true;
    }

    private static String hex(byte[] value) {
        char[] result = new char[value.length * 2];
        for (int index = 0; index < value.length; index++) {
            int current = value[index] & 0xff;
            result[index * 2] = HEX[current >>> 4];
            result[index * 2 + 1] = HEX[current & 0x0f];
        }
        return new String(result);
    }

    private void saveWorkspace() throws CoreException {
        workspace.save(true, monitor);
    }

    private void emitReady() {
        int builderCount = 0;
        try {
            for (ICommand command : project.getDescription().getBuildSpec()) {
                if (JavaCore.BUILDER_ID.equals(command.getBuilderName())) {
                    builderCount++;
                }
            }
        } catch (CoreException exception) {
            exception.printStackTrace(System.err);
        }
        IFolder source = project.getFolder("src");
        java.net.URI sourceLocation = source.getLocationURI();
        emit("{\"ok\":true,\"status\":\"ready\","
                + "\"application_id\":\"net.jolink.runtime.jdt.worker\","
                + "\"java_builder_id\":" + json(JavaCore.BUILDER_ID) + ","
                + "\"java_builder_count\":" + builderCount + ","
                + "\"jdt_bundle_version\":"
                + json(String.valueOf(
                        Platform.getBundle(JavaCore.PLUGIN_ID).getVersion())) + ","
                + "\"instrumentation\":"
                + json(instrumentationEnabled ? "enabled" : "disabled") + ","
                + "\"workspace_project_state\":"
                + json(projectReopened ? "reopened" : "created") + ","
                + "\"source_encoding_requested\":"
                + json(requestedSourceEncoding) + ","
                + "\"source_encoding_requested_canonical\":"
                + json(canonicalSourceEncoding) + ","
                + "\"source_encoding_effective\":"
                + json(effectiveSourceEncoding) + ","
                + "\"source_encoding_verified\":"
                + sourceEncodingVerified + ","
                + "\"source_level\":" + json(
                        javaProject.getOption(JavaCore.COMPILER_SOURCE, false)) + ","
                + "\"method_parameters\":" + json(
                        javaProject.getOption(
                                JavaCore.COMPILER_CODEGEN_METHOD_PARAMETERS_ATTR,
                                false)) + ","
                + "\"test_model_configured\":"
                + testModelConfigured + ","
                + "\"apt_enabled\":" + aptEnabled + ","
                + "\"apt_factory_path_requested_count\":"
                + aptFactoryPathRequestedCount + ","
                + "\"apt_factory_path_effective_count\":"
                + aptFactoryPathEffectiveCount + ","
                + "\"apt_factory_path_requested_identity\":"
                + jsonNullable(aptFactoryPathRequestedIdentity) + ","
                + "\"apt_factory_path_effective_identity\":"
                + jsonNullable(aptFactoryPathEffectiveIdentity) + ","
                + "\"apt_factory_path_verified\":"
                + aptFactoryPathVerified + ","
                + "\"apt_unexpected_enabled_container_count\":"
                + aptUnexpectedEnabledContainerCount + ","
                + "\"apt_unexpected_enabled_container_identity\":"
                + jsonNullable(aptUnexpectedEnabledContainerIdentity) + ","
                + "\"apt_generated_source_requested\":"
                + jsonNullable(aptGeneratedSourceRequested) + ","
                + "\"apt_generated_source_effective\":"
                + jsonNullable(aptGeneratedSourceEffective) + ","
                + "\"apt_generated_source_verified\":"
                + aptGeneratedSourceVerified + ","
                + "\"source_resource_full_path\":"
                + json(source.getFullPath().toString()) + ","
                + "\"source_location_uri\":"
                + (sourceLocation == null
                        ? "null" : json(sourceLocation.toASCIIString())) + ","
                + "\"evidence_status\":\"phase_1a_candidate\"}");
    }

    private void emitError(String code, String message) {
        emit("{\"ok\":false,\"error_code\":" + json(code)
                + ",\"message\":" + json(message) + "}");
    }

    private void emitBuildError(
            String code,
            String message,
            String requestId,
            String buildGenerationId) {
        emit("{\"ok\":false,\"error_code\":" + json(code)
                + ",\"message\":" + json(message)
                + identityFields(requestId, buildGenerationId) + "}");
    }

    private synchronized void emit(String frame) {
        protocol.println(frame);
        protocol.flush();
    }

    private static String jsonArray(List<String> values) {
        StringBuilder result = new StringBuilder("[");
        for (int index = 0; index < values.size(); index++) {
            if (index > 0) {
                result.append(',');
            }
            result.append(json(values.get(index)));
        }
        return result.append(']').toString();
    }

    private static String jsonObjectsArray(List<String> values) {
        return "[" + String.join(",", values) + "]";
    }

    private static String severityName(int severity) {
        if (severity == IMarker.SEVERITY_ERROR) {
            return "ERROR";
        }
        if (severity == IMarker.SEVERITY_WARNING) {
            return "WARNING";
        }
        if (severity == IMarker.SEVERITY_INFO) {
            return "INFO";
        }
        return "UNKNOWN";
    }

    private static String jsonNullable(String value) {
        return value == null ? "null" : json(value);
    }

    private static String json(String value) {
        StringBuilder result = new StringBuilder("\"");
        for (int index = 0; index < value.length(); index++) {
            char character = value.charAt(index);
            switch (character) {
                case '\\':
                    result.append("\\\\");
                    break;
                case '\"':
                    result.append("\\\"");
                    break;
                case '\n':
                    result.append("\\n");
                    break;
                case '\r':
                    result.append("\\r");
                    break;
                case '\t':
                    result.append("\\t");
                    break;
                default:
                    if (character < 0x20) {
                        result.append(String.format("\\u%04x", (int) character));
                    } else {
                        result.append(character);
                    }
            }
        }
        return result.append('\"').toString();
    }
}
