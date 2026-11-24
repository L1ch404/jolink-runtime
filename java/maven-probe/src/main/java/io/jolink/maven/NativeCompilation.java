package io.jolink.maven;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import javax.xml.parsers.DocumentBuilderFactory;
import java.util.*;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.lifecycle.internal.*;
import org.apache.maven.plugin.*;
import org.apache.maven.plugin.logging.Log;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

/** Execute only the compiler and the selected post-test executions. The Probe already
 * ran source/resource preparation; do not replay unrelated lifecycle side effects. */
final class NativeCompilation {
    static void execute(MavenSession session, MavenProject project, LifecycleExecutionPlanCalculator plans,
            BuildPluginManager plugins, File directory, boolean tests, Log log) throws MojoExecutionException {
        MavenProject previous = session.getCurrentProject();
        try {
            session.setCurrentProject(project);
            Set<String> post = new HashSet<>();
            Map<String, Element> refresh = new HashMap<>();
            DocumentBuilderFactory xml = DocumentBuilderFactory.newInstance();
            xml.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            NodeList modules = xml.newDocumentBuilder().parse(new File(directory, "compile-inputs.xml")).getElementsByTagName("module");
            for (int i=0; i<modules.getLength(); i++) {
                Element module = (Element) modules.item(i);
                if (!new File(module.getAttribute("root")).equals(project.getBasedir())) continue;
                NodeList entries = module.getElementsByTagName("*");
                for (int j=0; j<entries.getLength(); j++) {
                    Element item = (Element) entries.item(j);
                    if (item.hasAttribute("replaces")) {
                        List<String> roots = "main".equals(item.getTagName()) ? project.getCompileSourceRoots() : project.getTestCompileSourceRoots();
                        File old = new File(item.getAttribute("replaces")).getCanonicalFile();
                        for (Iterator<String> it = roots.iterator(); it.hasNext();)
                            if (new File(it.next()).getCanonicalFile().equals(old)) it.remove();
                    }
                    if ("main".equals(item.getTagName())) project.addCompileSourceRoot(item.getTextContent());
                    else if ("test".equals(item.getTagName())) project.addTestCompileSourceRoot(item.getTextContent());
                    else if ("post".equals(item.getTagName())) post.add(item.getTextContent());
                    else if ("refresh".equals(item.getTagName())) refresh.put(item.getAttribute("goal"), item);
                }
            }
            String last = !tests ? "compile" : post.isEmpty() ? "test-compile" : "process-test-classes";
            for (MojoExecution e : plans.calculateExecutionPlan(session, project,
                    Collections.<Object>singletonList(new LifecycleTask(last)), false).getMojoExecutions()) {
                String identity = e.getGroupId() + ":" + e.getArtifactId() + ":" + e.getGoal() + "@" + e.getExecutionId();
                boolean compiler = "org.apache.maven.plugins".equals(e.getGroupId()) && "maven-compiler-plugin".equals(e.getArtifactId())
                    && ("compile".equals(e.getGoal()) || (tests && "testCompile".equals(e.getGoal())));
                if (compiler || ("process-test-classes".equals(e.getLifecyclePhase()) && post.contains(identity))) {
                    plans.setupMojoExecution(session, project, e);
                    if (!e.getForkedExecutions().isEmpty()) throw new MojoExecutionException("Native compilation cannot replay a fork: " + identity);
                    if (compiler && refresh.containsKey(e.getGoal()))
                        refreshCompilerInputs(session, project, e, refresh.get(e.getGoal()), directory, log);
                    log.info("joLink native compilation: " + identity);
                    plugins.executeMojo(session, e);
                }
            }
        } catch (Exception ex) {
            throw new MojoExecutionException("Native compilation failed: " + ex.getMessage(), ex);
        } finally { session.setCurrentProject(previous); }
    }

    /** Maven's incremental compiler normally ignores removed classpath entries. Only
     * reset its input index for affected goals; Maven owns cleanup via createdFiles.lst. */
    private static void refreshCompilerInputs(MavenSession session, MavenProject project, MojoExecution e,
            Element change, File directory, Log log) throws Exception {
        Path build = new File(project.getBuild().getDirectory()).getCanonicalFile().toPath();
        Path base = build.resolve("maven-status/maven-compiler-plugin").resolve(e.getGoal());
        Path status = base.resolve(e.getExecutionId()).toFile().getCanonicalFile().toPath();
        if (!base.equals(status.getParent())) throw new MojoExecutionException("Cannot invalidate Maven compiler status outside " + base);
        Path inputs = status.resolve("inputFiles.lst"), outputs = status.resolve("createdFiles.lst");
        Xpp3Dom config = e.getConfiguration();
        Xpp3Dom incremental = config.getChild("useIncrementalCompilation");
        if (incremental == null) {
            incremental = new Xpp3Dom("useIncrementalCompilation");
            config.addChild(incremental);
        }
        Object setting = incremental.getValue() == null ? null
                : new PluginParameterExpressionEvaluator(session, e).evaluate(incremental.getValue());
        if (Boolean.parseBoolean(change.getAttribute("removed")) &&
                ("false".equalsIgnoreCase(String.valueOf(setting)) || !Files.isRegularFile(inputs) || !Files.isRegularFile(outputs))) {
            Path classes = new File("compile".equals(e.getGoal()) ? project.getBuild().getOutputDirectory()
                    : project.getBuild().getTestOutputDirectory()).toPath();
            if (Files.isDirectory(classes)) try (java.util.stream.Stream<Path> tree = Files.walk(classes)) {
                if (tree.anyMatch(p -> p.toString().endsWith(".class") && Files.isRegularFile(p))) {
                    String reason = "Old classes remain without reliable Maven compiler ownership records for "
                            + project.getId() + " " + e.getGoal() + "@" + e.getExecutionId()
                            + ". Run Maven clean for the affected module, then retry.";
                    Files.write(directory.toPath().resolve("source-removal-requires-clean.txt"), reason.getBytes(StandardCharsets.UTF_8));
                    throw new MojoExecutionException("MAVEN_NATIVE_SOURCE_REMOVAL_REQUIRES_CLEAN: " + reason);
                }
            }
        }
        // On an empty source set keep the previous list: it is how Maven notices
        // that the last input was removed. Never remove class files ourselves.
        if (Boolean.parseBoolean(change.getAttribute("sources"))) Files.deleteIfExists(inputs);
        incremental.setValue("true");
        log.info("joLink refreshed compiler inputs after source removal: " + e.getGoal() + "@" + e.getExecutionId());
    }
}
