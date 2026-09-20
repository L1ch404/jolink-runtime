package net.jolink.runtime.jdt;

import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.eclipse.core.resources.*;
import org.eclipse.core.runtime.*;
import static net.jolink.runtime.jdt.WorkerApplication.*;

/** Several Java projects in one workspace and one Worker JVM. */
final class ModuleWorkspace {
    final List<IProject> projects = new ArrayList<>();
    final Set<IProject> testProjects = new HashSet<>();
    boolean reopened;
    private IWorkspace workspace;

    void initialize(java.nio.file.Path specification, boolean reuse) throws Exception {
        Properties properties = new Properties();
        try (java.io.Reader reader = Files.newBufferedReader(specification, StandardCharsets.UTF_8)) {
            properties.load(reader);
        }
        workspace = ResourcesPlugin.getWorkspace();
        String[] names = properties.getProperty("modules").split(",");
        reopened = reuse;
        for (String name : names) {
            IProject project = workspace.getRoot().getProject(name);
            if (!project.exists()) { project.create(null); reopened = false; }
            if (!project.isOpen()) project.open(null);
            projects.add(project);
            if ("test".equals(properties.getProperty(name + ".scope"))) testProjects.add(project);
        }
        for (String name : names) {
            WorkerApplication module = new WorkerApplication();
            module.useProjectName(name);
            String prefix = name + ".";
            String processors = properties.getProperty(prefix + "processors");
            String tests = properties.getProperty(prefix + "tests");
            module.initialize(Paths.get(properties.getProperty(prefix + "classpath")),
                    properties.getProperty(prefix + "encoding"),
                    properties.getProperty(prefix + "level"),
                    Boolean.parseBoolean(properties.getProperty(prefix + "parameters")),
                    processors == null ? null : Paths.get(processors),
                    tests == null ? null : Paths.get(tests), reopened);
            module.configureProcessorSettings(properties.getProperty(prefix + "apt_settings"));
        }
    }

    String build(int kind, String requested, List<String> touched, NullProgressMonitor monitor) throws Exception {
        long started = System.nanoTime();
        BuildObservation.begin();
        Map<IProject, OutputChanges> observers = new LinkedHashMap<>();
        for (IProject project : projects) {
            OutputChanges observer = new OutputChanges(project);
            observers.put(project, observer);
            workspace.addResourceChangeListener(observer, IResourceChangeEvent.POST_BUILD);
        }
        List<String> removedSources = new ArrayList<>();
        try {
            if (touched == null) {
                for (IProject project : projects) project.refreshLocal(IResource.DEPTH_INFINITE, monitor);
            } else {
                Set<IContainer> parents = new LinkedHashSet<>();
                for (String path : touched) {
                    IFile file = workspace.getRoot().getFile(new org.eclipse.core.runtime.Path("/" + path));
                    parents.add(file.getParent());
                    if (!Files.isRegularFile(file.getLocation().toFile().toPath())) removedSources.add(path);
                }
                for (IContainer parent : parents) parent.refreshLocal(IResource.DEPTH_ONE, monitor);
                for (String path : touched) {
                    IFile file = workspace.getRoot().getFile(new org.eclipse.core.runtime.Path("/" + path));
                    if (file.exists()) file.touch(monitor);
                }
            }
            workspace.build(kind, monitor);
        } finally {
            for (OutputChanges observer : observers.values()) workspace.removeResourceChangeListener(observer);
        }
        double buildMs = (System.nanoTime() - started) / 1_000_000.0;
        long diagnosticStart = System.nanoTime();
        List<String> diagnostics = new ArrayList<>(), details = new ArrayList<>();
        int errors = 0, testErrors = 0;
        for (IProject project : projects) {
            for (IMarker marker : project.findMarkers(IMarker.PROBLEM, true, IResource.DEPTH_INFINITE)) {
                if (marker.getAttribute(IMarker.SEVERITY, -1) != IMarker.SEVERITY_ERROR) continue;
                errors++;
                ProblemDiagnostic diagnostic = new ProblemDiagnostic(marker);
                if (testProjects.contains(project) || diagnostic.resource.startsWith("test-src/")) testErrors++;
                if (details.size() < 128) {
                    details.add(diagnostic.detailJson().replace("\"resource\":", "\"module\":" + json(project.getName()) + ",\"resource\":"));
                    diagnostics.add(project.getName() + "/" + diagnostic.compact());
                }
            }
        }
        List<String> changed = new ArrayList<>(), deleted = new ArrayList<>(), resources = new ArrayList<>(), deletedResources = new ArrayList<>();
        for (Map.Entry<IProject, OutputChanges> entry : observers.entrySet()) {
            String prefix = entry.getKey().getName() + "/";
            for (String path : entry.getValue().paths(false, true)) changed.add(prefix + path);
            for (String path : entry.getValue().paths(true, true)) deleted.add(prefix + path);
            for (String path : entry.getValue().paths(false, false)) resources.add(prefix + path);
            for (String path : entry.getValue().paths(true, false)) deletedResources.add(prefix + path);
        }
        BuildObservation.Snapshot observed = BuildObservation.snapshot();
        return "{\"ok\":" + (errors == 0) + ",\"operation_ok\":true,\"compile_ok\":" + (errors == 0)
                + ",\"actual_build_kind\":" + (observed.actualBuildKind() == null ? "null" : json(observed.actualBuildKind()))
                + ",\"elapsed_ms\":" + buildMs + ",\"diagnostics_ms\":" + ((System.nanoTime()-diagnosticStart)/1_000_000.0)
                + ",\"error_count\":" + errors + ",\"warning_count\":0,\"main_error_count\":" + (errors-testErrors)
                + ",\"test_error_count\":" + testErrors + ",\"main_compile_ok\":" + (errors==testErrors)
                + ",\"test_compile_ok\":" + (testErrors==0)
                + ",\"compiled_source_units\":" + jsonArray(observed.compiledUnits)
                + ",\"deleted_source_units\":" + jsonArray(removedSources)
                + ",\"changed_classes\":" + jsonArray(changed) + ",\"deleted_classes\":" + jsonArray(deleted)
                + ",\"changed_resources\":" + jsonArray(resources) + ",\"deleted_resources\":" + jsonArray(deletedResources)
                + ",\"diagnostics\":" + jsonArray(diagnostics) + ",\"diagnostic_details\":" + jsonObjectsArray(details)
                + ",\"build_diagnostics\":" + BuildDecisionTrace.snapshotJson()
                + ",\"diagnostics_truncated\":" + (errors>details.size()) + "}";
    }
}
