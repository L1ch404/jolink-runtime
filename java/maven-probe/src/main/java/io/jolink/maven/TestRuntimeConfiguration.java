package io.jolink.maven;

import java.io.File;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Properties;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.MojoExecutionException;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Resolve each test execution independently; selecting tests never changes the compiler world. */
final class TestRuntimeConfiguration {
    String workingDirectory;
    boolean enableAssertions;
    final Map<String, String> environment = new LinkedHashMap<>();

    static void appendExecutions(StringBuilder out, MavenProject project, MavenSession session)
            throws MojoExecutionException {
        MavenProject previous = session.getCurrentProject();
        try {
            session.setCurrentProject(project);
            out.append("[ ");
            boolean comma = false;
            for (String kind : new String[] {"surefire", "failsafe"}) {
                String artifactId = "maven-" + kind + "-plugin";
                String goal = "surefire".equals(kind) ? "test" : "integration-test";
                Plugin plugin = project.getPlugin("org.apache.maven.plugins:" + artifactId);
                if (plugin == null && "failsafe".equals(kind)) continue;
                if (plugin == null) {
                    plugin = new Plugin();
                    plugin.setGroupId("org.apache.maven.plugins");
                    plugin.setArtifactId(artifactId);
                }
                List<String> ids = new ArrayList<>();
                for (PluginExecution execution : plugin.getExecutions())
                    if (execution.getGoals().contains(goal)) ids.add(execution.getId());
                if (ids.isEmpty() && "surefire".equals(kind)) ids.add("default-test");
                for (String id : ids) {
                    if (comma) out.append(',');
                    comma = true;
                    out.append('{');
                    ExportBuildWorldMojo.field(out, "id", artifactId + ":" + id, false);
                    ExportBuildWorldMojo.field(out, "kind", kind, true);
                    try {
                        append(out, project, session, plugin, id, goal, kind);
                    } catch (Exception error) {
                        // An unrelated execution's invalid runtime setup must not block other tests or launch.
                        ExportBuildWorldMojo.field(out, "configurationError", error.getClass().getSimpleName(), true);
                    }
                    out.append('}');
                }
            }
            out.append(']');
        } finally {
            session.setCurrentProject(previous);
        }
    }

    private static void append(StringBuilder out, MavenProject project, MavenSession session,
            Plugin plugin, String id, String goal, String kind) throws Exception {
            Xpp3Dom raw = project.getGoalConfiguration(plugin.getGroupId(), plugin.getArtifactId(), id, goal);
            Xpp3Dom config = raw == null ? new Xpp3Dom("configuration") : new Xpp3Dom(raw);
            PluginParameterExpressionEvaluator evaluator = new PluginParameterExpressionEvaluator(session, new MojoExecution(plugin, goal, id));
            resolve(config, evaluator);
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
            List<String> files = new ArrayList<>();
            patterns(config, "includes", "include", "surefire".equals(kind)
                ? new String[] {"**/Test*.java", "**/*Test.java", "**/*Tests.java", "**/*TestCase.java"}
                : new String[] {"**/IT*.java", "**/*IT.java", "**/*ITCase.java"}, evaluator, files);
            patterns(config, "excludes", "exclude", new String[] {"**/*$*"}, evaluator, files);
            ExportBuildWorldMojo.field(out, "configurationPrivate", config.toString(), true);
            Map<String, String> properties = new LinkedHashMap<>();
            String propertyFile = value(config, "systemPropertiesFile", null);
            if (propertyFile != null) files.add(evaluator.alignToBaseDirectory(new File(propertyFile)).getCanonicalPath());
            ExportBuildWorldMojo.stringList(out, "configurationFiles", files, true);
            if (propertyFile != null) {
                File file = evaluator.alignToBaseDirectory(new File(propertyFile));
                Properties loaded = new Properties();
                try (InputStream input = Files.newInputStream(file.toPath())) { loaded.load(input); }
                for (String name : loaded.stringPropertyNames()) properties.put(name, loaded.getProperty(name));
            }
            ExportBuildWorldMojo.field(out, "workingDirectory", result.workingDirectory, true);
            ExportBuildWorldMojo.booleanField(out, "enableAssertions", result.enableAssertions, true);
            map(out, "environmentOverridesPrivate", result.environment);
            map(out, "fileSystemPropertiesPrivate", properties);
            ExportBuildWorldMojo.booleanField(out, "pluginDependencies", !plugin.getDependencies().isEmpty(), true);
    }

    private static void patterns(Xpp3Dom config, String name, String itemName, String[] defaults,
            PluginParameterExpressionEvaluator evaluator, List<String> files) throws Exception {
        Xpp3Dom patterns = child(config, name);
        if (patterns == null) { patterns = new Xpp3Dom(name); config.addChild(patterns); }
        String path = value(config, name + "File", null);
        if (path != null) {
            File file = evaluator.alignToBaseDirectory(new File(path));
            files.add(file.getCanonicalPath());
            for (String line : Files.readAllLines(file.toPath(), StandardCharsets.UTF_8)) {
                if (!line.trim().isEmpty() && !line.trim().startsWith("#")) add(patterns, itemName, line.trim());
            }
        }
        if (patterns.getChildCount() == 0) for (String pattern : defaults) add(patterns, itemName, pattern);
    }

    private static void add(Xpp3Dom node, String name, String value) {
        Xpp3Dom child = new Xpp3Dom(name); child.setValue(value); node.addChild(child);
    }

    private static void resolve(Xpp3Dom config, PluginParameterExpressionEvaluator evaluator) throws Exception {
        for (Xpp3Dom node : config.getChildren()) {
            if ("workingDirectory".equals(node.getName())) continue;
            if (node.getChildCount() > 0) { resolve(node, evaluator); continue; }
            if (node.getValue() == null) continue;
            Object resolved = evaluator.evaluate(node.getValue());
            String text = resolved == null ? "" : resolved.toString();
            if ("argLine".equals(node.getName())) {
                Matcher late = Pattern.compile("@\\{([^}]+)\\}").matcher(text);
                StringBuffer output = new StringBuffer();
                while (late.find()) {
                    Object property = evaluator.evaluate("${" + late.group(1) + "}");
                    late.appendReplacement(output, Matcher.quoteReplacement(property == null ? "" : property.toString()));
                }
                late.appendTail(output);
                text = output.toString().replace("${argLine}", "").replace("${surefire.forkNumber}", "1");
            }
            node.setValue(text);
        }
    }

    private static void map(StringBuilder out, String name, Map<String, String> values) {
        out.append(',').append('"').append(name).append("\":{");
        boolean comma = false;
        for (Map.Entry<String, String> entry : values.entrySet()) {
            ExportBuildWorldMojo.field(out, entry.getKey(), entry.getValue(), comma);
            comma = true;
        }
        out.append('}');
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
