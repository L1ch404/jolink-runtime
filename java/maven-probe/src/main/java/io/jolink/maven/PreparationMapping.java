package io.jolink.maven;

import java.io.*;
import java.util.*;
import java.util.jar.JarFile;
import java.util.zip.ZipEntry;
import javax.xml.parsers.DocumentBuilderFactory;
import org.apache.maven.artifact.Artifact;
import org.apache.maven.artifact.versioning.DefaultArtifactVersion;
import org.apache.maven.artifact.versioning.VersionRange;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Plugin;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;
import org.w3c.dom.Element;
import org.w3c.dom.Node;

/** Consume m2e execution declarations without embedding Eclipse or guessing goal names. */
final class PreparationMapping {
    private static final String EMBEDDED = "META-INF/m2e/lifecycle-mapping-metadata.xml";
    private final Map<File, Xpp3Dom> embedded = new HashMap<>();
    private final Xpp3Dom defaults;

    PreparationMapping() throws Exception {
        try (InputStream input = getClass().getResourceAsStream("/META-INF/jolink/preparation-mapping.xml")) {
            defaults = read(input);
        }
    }

    String skipReason(MavenSession session, MavenProject project, MojoExecution execution) throws Exception {
        PluginParameterExpressionEvaluator evaluator = new PluginParameterExpressionEvaluator(session, execution);
        // Maven has already merged active profiles, parent configuration and properties.
        if (project.getPluginManagement() != null) {
            for (Plugin plugin : project.getPluginManagement().getPlugins()) {
                if ("org.eclipse.m2e".equals(plugin.getGroupId()) && "lifecycle-mapping".equals(plugin.getArtifactId())) {
                    Xpp3Dom config = (Xpp3Dom) plugin.getConfiguration();
                    String action = action(child(config, "lifecycleMappingMetadata"), execution, evaluator, false);
                    if (action != null) return "ignore".equals(action) ? "project_m2e_mapping" : null;
                }
            }
        }
        Artifact artifact = execution.getMojoDescriptor().getPluginDescriptor().getPluginArtifact();
        File file = artifact == null ? null : artifact.getFile();
        if (file != null && file.isFile()) {
            if (!embedded.containsKey(file)) {
                try (JarFile jar = new JarFile(file)) {
                    ZipEntry entry = jar.getJarEntry(EMBEDDED);
                    try (InputStream input = entry == null ? null : jar.getInputStream(entry)) {
                        embedded.put(file, read(input));
                    }
                }
            }
            String action = action(embedded.get(file), execution, evaluator, true);
            if (action != null) return "ignore".equals(action) ? "plugin_m2e_mapping" : null;
        }
        return "ignore".equals(action(defaults, execution, evaluator, false)) ? "jolink_default_mapping" : null;
    }

    private static String action(Xpp3Dom metadata, MojoExecution execution,
            PluginParameterExpressionEvaluator evaluator, boolean localPlugin) throws Exception {
        Xpp3Dom entries = child(metadata, "pluginExecutions");
        if (entries == null) return null;
        for (Xpp3Dom entry : entries.getChildren("pluginExecution")) {
            Xpp3Dom filter = child(entry, "pluginExecutionFilter");
            if (filter == null) continue;
            String group = text(filter, "groupId"), artifact = text(filter, "artifactId");
            if (!(localPlugin && group == null) && !execution.getGroupId().equals(group)) continue;
            if (!(localPlugin && artifact == null) && !execution.getArtifactId().equals(artifact)) continue;
            String version = text(filter, "versionRange");
            if (version != null) {
                VersionRange range = VersionRange.createFromVersionSpec(version);
                if (!range.containsVersion(new DefaultArtifactVersion(execution.getVersion()))) continue;
            } else if (!localPlugin) continue;
            Xpp3Dom goals = child(filter, "goals");
            if (goals == null || !Arrays.stream(goals.getChildren("goal"))
                    .anyMatch(goal -> execution.getGoal().equals(goal.getValue()))) continue;
            Xpp3Dom parameters = child(filter, "parameters");
            boolean matches = true;
            if (parameters != null) for (Xpp3Dom parameter : parameters.getChildren()) {
                Xpp3Dom actual = child(execution.getConfiguration(), parameter.getName());
                String value = actual == null ? null : actual.getValue();
                if (value == null && actual != null) value = actual.getAttribute("default-value");
                Object resolved = value == null ? null : evaluator.evaluate(value);
                if (!Objects.equals(parameter.getValue(), resolved == null ? null : resolved.toString())) matches = false;
            }
            if (!matches) continue;
            Xpp3Dom action = child(entry, "action");
            if (action != null && action.getChildCount() > 0) return action.getChild(0).getName();
        }
        return null;
    }

    private static Xpp3Dom read(InputStream input) throws Exception {
        if (input == null) return null;
        DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
        factory.setNamespaceAware(true);
        factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        return dom(factory.newDocumentBuilder().parse(input).getDocumentElement());
    }

    private static Xpp3Dom dom(Element element) {
        Xpp3Dom result = new Xpp3Dom(element.getLocalName());
        for (Node node = element.getFirstChild(); node != null; node = node.getNextSibling()) {
            if (node instanceof Element) result.addChild(dom((Element) node));
        }
        if (result.getChildCount() == 0) result.setValue(element.getTextContent().trim());
        return result;
    }

    private static Xpp3Dom child(Xpp3Dom node, String name) {
        return node == null ? null : node.getChild(name);
    }

    private static String text(Xpp3Dom node, String name) {
        Xpp3Dom value = child(node, name);
        return value == null ? null : value.getValue();
    }
}
