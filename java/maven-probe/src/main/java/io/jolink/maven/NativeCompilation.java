package io.jolink.maven;

import java.io.*;
import javax.xml.parsers.DocumentBuilderFactory;
import java.util.*;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.lifecycle.internal.*;
import org.apache.maven.plugin.*;
import org.apache.maven.plugin.logging.Log;
import org.apache.maven.project.MavenProject;
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
            DocumentBuilderFactory xml = DocumentBuilderFactory.newInstance();
            xml.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            NodeList modules = xml.newDocumentBuilder().parse(new File(directory, "compile-inputs.xml")).getElementsByTagName("module");
            for (int i=0; i<modules.getLength(); i++) {
                Element module = (Element) modules.item(i);
                if (!new File(module.getAttribute("root")).equals(project.getBasedir())) continue;
                NodeList entries = module.getElementsByTagName("*");
                for (int j=0; j<entries.getLength(); j++) {
                    Element item = (Element) entries.item(j);
                    if ("main".equals(item.getTagName())) project.addCompileSourceRoot(item.getTextContent());
                    else if ("test".equals(item.getTagName())) project.addTestCompileSourceRoot(item.getTextContent());
                    else if ("post".equals(item.getTagName())) post.add(item.getTextContent());
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
                    log.info("joLink native compilation: " + identity);
                    plugins.executeMojo(session, e);
                }
            }
        } catch (Exception ex) {
            throw new MojoExecutionException("Native compilation failed: " + ex.getMessage(), ex);
        } finally { session.setCurrentProject(previous); }
    }
}
