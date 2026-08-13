package net.jolink.runtime.jdt;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

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
import org.eclipse.jdt.core.IJavaProject;
import org.eclipse.jdt.core.JavaCore;

/**
 * Small protocol worker used only by the isolated Phase 1A experiment.
 *
 * <p>The original synchronous BUILD/SAVE/STOP protocol remains available for
 * A1-A8. A9 additionally uses identity-bound asynchronous build, status,
 * cancellation, barrier, metrics, and GC commands. Stdout contains JSON
 * protocol frames only. Diagnostics belong on stderr.</p>
 */
public final class WorkerApplication implements IApplication {
    private static final String PROJECT_NAME = "plain-fixture";
    private static final int MAX_DIAGNOSTICS = 64;

    private final NullProgressMonitor monitor = new NullProgressMonitor();
    private PrintWriter protocol;
    private IWorkspace workspace;
    private IProject project;
    private IJavaProject javaProject;
    private boolean instrumentationEnabled;
    private boolean projectReopened;
    private ActiveBuild activeBuild;
    private String lastTerminalRequestId;
    private String lastTerminalBuildGenerationId;
    private String lastTerminalStatus;
    private long protocolSequence;

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

    @Override
    public Object start(IApplicationContext context) throws Exception {
        protocol = new PrintWriter(
                new java.io.OutputStreamWriter(System.out, StandardCharsets.UTF_8),
                true);
        Map<String, String> arguments = parseArguments(context);
        String systemLibrariesFile = arguments.get("system-libraries");
        if (systemLibrariesFile == null || systemLibrariesFile.isBlank()) {
            emitError("MISSING_SYSTEM_LIBRARIES", "Missing --system-libraries.");
            return Integer.valueOf(2);
        }
        instrumentationEnabled = !"disabled".equals(
                arguments.getOrDefault("instrumentation", "enabled"));
        BuildObservation.setEnabled(instrumentationEnabled);

        try {
            initialize(Path.of(systemLibrariesFile));
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

    private void initialize(Path systemLibrariesFile) throws Exception {
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
        IFolder output = ensureFolder(project.getFolder("bin"));
        javaProject = JavaCore.create(project);
        if (!output.getFullPath().equals(javaProject.getOutputLocation())) {
            javaProject.setOutputLocation(output.getFullPath(), monitor);
        }

        List<IClasspathEntry> classpath = new ArrayList<>();
        classpath.add(JavaCore.newSourceEntry(source.getFullPath()));
        for (String line : Files.readAllLines(
                systemLibrariesFile, StandardCharsets.UTF_8)) {
            String value = line.trim();
            if (!value.isEmpty()) {
                Path entry = Path.of(value);
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
        if (classpath.size() == 1) {
            throw new IOException("System library snapshot is empty.");
        }
        IClasspathEntry[] desiredClasspath = classpath.toArray(IClasspathEntry[]::new);
        if (!Arrays.equals(javaProject.getRawClasspath(), desiredClasspath)) {
            javaProject.setRawClasspath(desiredClasspath, monitor);
        }

        Map<String, String> options = new LinkedHashMap<>(
                javaProject.getOptions(false));
        JavaCore.setComplianceOptions(JavaCore.VERSION_1_8, options);
        options.put(JavaCore.COMPILER_SOURCE, JavaCore.VERSION_1_8);
        options.put(JavaCore.COMPILER_COMPLIANCE, JavaCore.VERSION_1_8);
        options.put(JavaCore.COMPILER_CODEGEN_TARGET_PLATFORM, JavaCore.VERSION_1_8);
        options.put(JavaCore.COMPILER_PB_ENABLE_PREVIEW_FEATURES, JavaCore.DISABLED);
        if (!javaProject.getOptions(false).equals(options)) {
            javaProject.setOptions(options);
        }
        project.refreshLocal(IResource.DEPTH_INFINITE, monitor);
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
        List<String> diagnostics = new ArrayList<>();
        List<String> diagnosticDetails = new ArrayList<>();
        int errorCount = 0;
        for (IMarker marker : markers) {
            int severity = marker.getAttribute(IMarker.SEVERITY, -1);
            if (severity == IMarker.SEVERITY_ERROR) {
                errorCount++;
            }
            if (diagnostics.size() < MAX_DIAGNOSTICS) {
                String resource = marker.getResource().getProjectRelativePath().toString();
                int line = marker.getAttribute(IMarker.LINE_NUMBER, -1);
                String message = marker.getAttribute(IMarker.MESSAGE, "");
                diagnostics.add(resource + ":" + line + ":" + severity + ":" + message);
                int characterStart = marker.getAttribute(IMarker.CHAR_START, -1);
                int characterEnd = marker.getAttribute(IMarker.CHAR_END, -1);
                diagnosticDetails.add("{\"resource\":" + json(resource)
                        + ",\"line\":" + line
                        + ",\"severity\":" + severity
                        + ",\"severity_name\":" + json(severityName(severity))
                        + ",\"character_start\":" + characterStart
                        + ",\"character_end\":" + characterEnd
                        + ",\"message\":" + json(message) + "}");
            }
        }

        String actualBuildKind = observation.actualBuildKind();
        boolean compileOperation = !"CLEAN".equals(requestedKind);
        boolean compileOk = errorCount == 0;
        boolean compilerOutputEligible = compileOperation && compileOk;
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
                .append("}")
                .append(",\"resource_delta\":{")
                .append("\"status\":\"unavailable\",")
                .append("\"reason\":\"resource_delta_instrumentation_not_implemented\"}")
                .append(",\"observer_build_finished\":")
                .append(observation.buildFinished)
                .append(",\"compiled_source_units\":")
                .append(jsonArray(observation.compiledUnits))
                .append(",\"elapsed_ms\":").append(elapsedMillis)
                .append(",\"error_count\":").append(errorCount)
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
                .append(markers.length > MAX_DIAGNOSTICS)
                .append(",\"metrics\":")
                .append(WorkerMetrics.snapshotJson(false));
        if (active != null) {
            result.append(identityFields(
                    active.requestId, active.buildGenerationId))
                    .append(",\"terminal_record_source\":\"worker\"");
        }
        result
                .append("}");
        if (active == null) {
            emit(result.toString());
        } else {
            emitTerminal(active, result.toString());
        }
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
                        Path path = Path.of(location.toOSString());
                        String relative = location
                                .makeRelativeTo(output.getLocation())
                                .toString();
                        hashes.put(relative, sha256(path));
                    } catch (Exception exception) {
                        throw new CoreException(
                                org.eclipse.core.runtime.Status.error(
                                        "Unable to hash class output.", exception));
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
        return java.util.HexFormat.of().formatHex(digest.digest());
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
