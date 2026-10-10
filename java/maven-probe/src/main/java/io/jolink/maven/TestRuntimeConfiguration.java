package io.jolink.maven;

import java.io.File;
import java.util.LinkedHashMap;
import java.util.Map;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Runtime-only Surefire settings, resolved by Maven in the selected module. */
final class TestRuntimeConfiguration {
    String workingDirectory;
    boolean enableAssertions;
    final Map<String, String> environment = new LinkedHashMap<>();

    static TestRuntimeConfiguration read(MavenProject project, MavenSession session, Plugin plugin)
            throws MojoExecutionException {
        MavenProject previous = session.getCurrentProject();
        try {
            session.setCurrentProject(project);
            if (plugin == null) {
                plugin = new Plugin();
                plugin.setGroupId("org.apache.maven.plugins");
                plugin.setArtifactId("maven-surefire-plugin");
            }
            String id = "default-test";
            for (PluginExecution execution : plugin.getExecutions()) {
                if (execution.getGoals().contains("test")) id = execution.getId();
            }
            Xpp3Dom config = project.getGoalConfiguration(plugin.getGroupId(), plugin.getArtifactId(), id, "test");
            PluginParameterExpressionEvaluator evaluator = new PluginParameterExpressionEvaluator(
                session, new MojoExecution(plugin, "test", id));
            TestRuntimeConfiguration result = new TestRuntimeConfiguration();
            Object directory = evaluator.evaluate(value(config, "workingDirectory", "${basedir}"));
            if (directory != null) {
                File file = directory instanceof File ? (File) directory : new File(String.valueOf(directory));
                result.workingDirectory = evaluator.alignToBaseDirectory(file).getCanonicalPath();
            }
            Object assertions = evaluator.evaluate(value(config, "enableAssertions", "${enableAssertions}"));
            result.enableAssertions = assertions == null || Boolean.parseBoolean(String.valueOf(assertions));
            Xpp3Dom excluded = child(config, "excludedEnvironmentVariables");
            if (excluded != null && excluded.getChildCount() > 0) {
                for (Xpp3Dom item : excluded.getChildren()) exclude(result, evaluator.evaluate(item.getValue()));
            } else {
                exclude(result, evaluator.evaluate(value(config, "excludedEnvironmentVariables", "${surefire.excludedEnvironmentVariables}")));
            }
            Xpp3Dom variables = child(config, "environmentVariables");
            if (variables != null) {
                for (Xpp3Dom item : variables.getChildren()) {
                    Object resolved = evaluator.evaluate(item.getValue());
                    // Surefire removes inherited names first, then applies explicit values.
                    result.environment.put(item.getName(), resolved == null ? "" : String.valueOf(resolved));
                }
            }
            return result;
        } catch (Exception error) {
            throw new MojoExecutionException("Unable to resolve test runtime configuration", error);
        } finally {
            session.setCurrentProject(previous);
        }
    }

    private static void exclude(TestRuntimeConfiguration target, Object resolved) {
        if (resolved == null) return;
        for (String name : String.valueOf(resolved).split(",")) {
            if (!name.trim().isEmpty()) target.environment.put(name.trim(), null);
        }
    }

    private static Xpp3Dom child(Xpp3Dom config, String name) {
        return config == null ? null : config.getChild(name);
    }

    private static String value(Xpp3Dom config, String name, String fallback) {
        Xpp3Dom item = child(config, name);
        return item == null || item.getValue() == null || item.getValue().trim().isEmpty() ? fallback : item.getValue();
    }
}
