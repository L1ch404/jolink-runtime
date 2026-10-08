package io.jolink.maven;

import java.io.File;
import java.nio.file.Path;
import java.util.*;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.lifecycle.internal.LifecycleExecutionPlanCalculator;
import org.apache.maven.lifecycle.internal.LifecycleTask;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.plugin.BuildPluginManager;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.plugin.logging.Log;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Run declared preparation and native resources, not the compile/test lifecycle. */
final class SourcePreparation {
    static final String INPUTS = "jolink.preparation.inputs";
    static final String EXECUTIONS = "jolink.preparation.executions";
    static final String ROOTS = "jolink.preparation.roots";
    private static final Set<String> MAIN = new HashSet<>(Arrays.asList(
        "generate-sources", "process-sources", "generate-resources", "process-resources"));
    private static final Set<String> TEST = new HashSet<>(Arrays.asList(
        "generate-test-sources", "process-test-sources", "generate-test-resources", "process-test-resources"));
    private final LifecycleExecutionPlanCalculator plans;
    private final BuildPluginManager plugins;
    private final Log log;

    SourcePreparation(LifecycleExecutionPlanCalculator plans, BuildPluginManager plugins, Log log) {
        this.plans = plans; this.plugins = plugins; this.log = log;
    }

    void execute(MavenSession session, MavenProject project, boolean tests) throws MojoExecutionException {
        MavenProject previous = session.getCurrentProject();
        Set<String> inputs = new LinkedHashSet<>();
        Set<String> registrations = new LinkedHashSet<>();
        List<String> executions = new ArrayList<>();
        try (ResourcePreparation resources = new ResourcePreparation()) {
            session.setCurrentProject(project);
            List<Object> tasks = Collections.singletonList(new LifecycleTask(tests ? "process-test-resources" : "process-resources"));
            for (MojoExecution execution : plans.calculateExecutionPlan(session, project, tasks, false).getMojoExecutions()) {
                String phase = execution.getLifecyclePhase();
                boolean resource = ResourcePreparation.handles(execution);
                if ((!MAIN.contains(phase) && !(tests && TEST.contains(phase))) || !(resource || isDeclared(project, execution))) continue;
                plans.setupMojoExecution(session, project, execution);
                // A fork may re-enter compile/test. Do not silently run or omit
                // it while promising that business compilation stays in JDT.
                if (!execution.getForkedExecutions().isEmpty())
                    throw new MojoExecutionException("Source preparation requires a forked lifecycle: " + execution.getGoal());
                Set<String> paths = new LinkedHashSet<>();
                Set<String> rootsBefore = sourceRoots(project);
                collectPaths(execution.getConfiguration(), new PluginParameterExpressionEvaluator(session, execution), project, paths);
                String identity = execution.getGroupId() + ":" + execution.getArtifactId() + ":" + execution.getGoal() + "@" + execution.getExecutionId();
                log.info("joLink source preparation: " + identity + " (" + phase + ")");
                if (resource) resources.execute(session, project, execution, plugins);
                else plugins.executeMojo(session, execution);
                // Include newly created output/source roots in the same small
                // file-input snapshot, so deleting generated outputs refreshes it.
                collectPaths(execution.getConfiguration(), new PluginParameterExpressionEvaluator(session, execution), project, paths);
                if (resource) {
                    // Native output may also contain javac classes. Track only
                    // resource inputs and the individual outputs we produced.
                    Xpp3Dom output = execution.getConfiguration().getChild("outputDirectory");
                    Set<String> destinations = new LinkedHashSet<>();
                    String value = output.getValue() == null ? output.getAttribute("default-value") : output.getValue();
                    collectPath(value, true, new PluginParameterExpressionEvaluator(session, execution), project, destinations);
                    paths.removeAll(destinations);
                }
                Set<String> addedRoots = sourceRoots(project);
                addedRoots.removeAll(rootsBefore);
                // If all file parameters merely register new Java roots,
                // their Java changes belong to JDT, not another Maven launch.
                if (!addedRoots.isEmpty() && addedRoots.containsAll(paths)) registrations.addAll(addedRoots);
                else inputs.addAll(paths);
                executions.add(identity);
            }
            resources.finish(project, inputs);
            List<String> inputPaths = new ArrayList<>(inputs);
            List<String> rootPaths = new ArrayList<>(registrations);
            Collections.sort(inputPaths);
            Collections.sort(rootPaths);
            project.setContextValue(INPUTS, inputPaths);
            project.setContextValue(EXECUTIONS, executions);
            project.setContextValue(ROOTS, rootPaths);
        } catch (MojoExecutionException error) {
            throw error;
        } catch (Exception error) {
            throw new MojoExecutionException("Maven source preparation failed", error);
        } finally {
            session.setCurrentProject(previous);
        }
    }

    private static boolean isDeclared(MavenProject project, MojoExecution execution) {
        for (Plugin plugin : project.getBuildPlugins()) {
            if (!plugin.getGroupId().equals(execution.getGroupId()) || !plugin.getArtifactId().equals(execution.getArtifactId())) continue;
            for (PluginExecution declared : plugin.getExecutions()) {
                if (hasDeclaration(declared) && declared.getId().equals(execution.getExecutionId()) && declared.getGoals().contains(execution.getGoal())) return true;
            }
        }
        return false;
    }

    private static boolean hasDeclaration(PluginExecution execution) {
        // Maven retains the POM location through inheritance/profile merging.
        // Injected packaging defaults have no positive POM source line.
        org.apache.maven.model.InputLocation location = execution.getLocation("");
        return location != null && location.getLineNumber() > 0;
    }

    private static Set<String> sourceRoots(MavenProject project) throws java.io.IOException {
        Set<String> roots = new LinkedHashSet<>();
        for (String path : project.getCompileSourceRoots()) roots.add(new File(path).getCanonicalPath());
        for (String path : project.getTestCompileSourceRoots()) roots.add(new File(path).getCanonicalPath());
        return roots;
    }

    private static void collectPaths(Xpp3Dom configuration, PluginParameterExpressionEvaluator evaluator,
            MavenProject project, Set<String> paths) throws Exception {
        if (configuration == null) return;
        for (Xpp3Dom child : configuration.getChildren()) {
            for (String attribute : child.getAttributeNames()) {
                if (!"implementation".equals(attribute) && !"default-value".equals(attribute))
                    collectPath(child.getAttribute(attribute), false, evaluator, project, paths);
            }
            if (child.getChildCount() > 0) { collectPaths(child, evaluator, project, paths); continue; }
            String value = child.getValue();
            if (value == null) value = child.getAttribute("default-value");
            collectPath(value, "java.io.File".equals(child.getAttribute("implementation")), evaluator, project, paths);
        }
    }

    private static void collectPath(String value, boolean fileParameter, PluginParameterExpressionEvaluator evaluator,
            MavenProject project, Set<String> paths) throws Exception {
        if (value == null) return;
        Object evaluated = evaluator.evaluate(value);
        if (evaluated instanceof Collection<?>) {
            for (Object item : (Collection<?>) evaluated) {
                if (item instanceof org.apache.maven.model.Resource) {
                    collectPath(((org.apache.maven.model.Resource) item).getDirectory(), true, evaluator, project, paths);
                } else if (item instanceof String || item instanceof File) {
                    collectPath(item.toString(), true, evaluator, project, paths);
                }
            }
            return;
        }
        if (!(evaluated instanceof String) && !(evaluated instanceof File)) return;
        File file = evaluator.alignToBaseDirectory(evaluated instanceof File ? (File)evaluated : new File((String)evaluated));
        // Pattern/enum/text options are not paths. Check existence before
        // canonicalizing them (notably wildcard patterns on Windows).
        if (!file.exists() && !fileParameter && !(evaluated instanceof File)) return;
        Path path = file.getCanonicalFile().toPath();
        // A context/basedir value must not become a scan of the entire repo.
        if (project.getBasedir().getCanonicalFile().toPath().startsWith(path)) return;
        paths.add(path.toString());
    }
}
