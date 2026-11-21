package io.jolink.maven;

import java.io.File;
import java.util.*;
import org.apache.maven.model.Resource;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.plugin.PluginParameterExpressionEvaluator;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** The in-place replacer:replace list mode uses Ant's DirectoryScanner, not all files in basedir. */
final class ReplacerInputs {
    final Xpp3Dom otherConfiguration;
    final Resource selection = new Resource();

    private ReplacerInputs(Xpp3Dom configuration) {
        otherConfiguration = new Xpp3Dom(configuration);
        Set<String> selectors = new HashSet<>(Arrays.asList("basedir", "includes", "excludes", "filesToInclude", "filesToExclude"));
        for (int i = otherConfiguration.getChildCount() - 1; i >= 0; i--)
            if (selectors.contains(otherConfiguration.getChild(i).getName())) otherConfiguration.removeChild(i);
    }

    static ReplacerInputs describe(MojoExecution e, PluginParameterExpressionEvaluator evaluator) throws Exception {
        if (!"com.google.code.maven-replacer-plugin".equals(e.getGroupId()) ||
            !"replacer".equals(e.getArtifactId()) || !"replace".equals(e.getGoal())) return null;
        Xpp3Dom config = e.getConfiguration();
        // Auxiliary files and redirected outputs may themselves depend on basedir.
        // Keep the existing broad model when this is not an in-place selection.
        if (hasPathOption(config)) return null;
        String base = scalar(config.getChild("basedir"), evaluator);
        if (base == null || !new File(base).isAbsolute()) return null;
        List<String> includes = patterns(config, "includes", "filesToInclude", evaluator);
        List<String> excludes = patterns(config, "excludes", "filesToExclude", evaluator);
        if (includes == null || includes.isEmpty() || excludes == null) return null;
        ReplacerInputs model = new ReplacerInputs(config);
        model.selection.setDirectory(new File(base).getCanonicalPath());
        model.selection.setIncludes(includes);
        model.selection.setExcludes(excludes);
        return model;
    }

    private static boolean hasPathOption(Xpp3Dom node) {
        for (Xpp3Dom child : node.getChildren()) {
            if (Arrays.asList("file","tokenFile","valueFile","tokenValueMap","outputDir","outputFile",
                    "outputBasedir","inputFilePattern","outputFilePattern").contains(child.getName())) return true;
            if (hasPathOption(child)) return true;
        }
        return false;
    }

    private static String scalar(Xpp3Dom node, PluginParameterExpressionEvaluator evaluator) throws Exception {
        if (node == null || node.getValue() == null) return null;
        Object v = evaluator.evaluate(node.getValue());
        return v instanceof String || v instanceof File ? v.toString() : null;
    }

    private static List<String> patterns(Xpp3Dom config, String listName, String csvName,
            PluginParameterExpressionEvaluator evaluator) throws Exception {
        List<String> result = new ArrayList<>();
        Xpp3Dom list = config.getChild(listName);
        if (list != null) {
            if (list.getValue() != null && list.getChildCount() == 0) return null;
            for (Xpp3Dom item : list.getChildren()) result.add(scalar(item, evaluator));
        }
        Xpp3Dom csv = config.getChild(csvName);
        if (csv != null) {
            String value = scalar(csv, evaluator);
            if (value == null) return null;
            for (String p : value.split(",")) result.add(p.trim());
        }
        for (String p : result) if (p == null || p.isEmpty() || p.contains("${") ||
                new File(p).isAbsolute() || Arrays.asList(p.replace('\\','/').split("/")).contains("..")) return null;
        return result;
    }

    void export(MojoExecution execution, ResourcePreparation resources) throws Exception {
        Class<?> scanner = execution.getMojoDescriptor().getPluginDescriptor().getClassRealm()
            .loadClass("org.apache.tools.ant.DirectoryScanner");
        for (String pattern : (String[]) scanner.getMethod("getDefaultExcludes").invoke(null))
            selection.addExclude(pattern);
        resources.addSelection(selection);
    }
}
