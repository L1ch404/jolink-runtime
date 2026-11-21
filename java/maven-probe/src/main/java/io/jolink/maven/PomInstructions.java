package io.jolink.maven;

import java.io.*;
import java.util.*;
import javax.xml.stream.*;
import org.apache.maven.model.InputLocation;
import org.apache.maven.model.InputLocationTracker;
import org.apache.maven.model.Plugin;
import org.apache.maven.plugin.MojoExecution;
import org.apache.maven.project.MavenProject;

/** Match processing instructions by Maven's declaration location, not plugin name. */
final class PomInstructions {
    static final class Directive {
        final File pom;
        final boolean ignore, incremental;
        Directive(File pom, boolean ignore, boolean incremental) {
            this.pom = pom; this.ignore = ignore; this.incremental = incremental;
        }
    }
    private final Map<File, Map<String, Directive>> cache = new HashMap<>();
    Directive find(MavenProject project, MojoExecution execution) throws Exception {
        String key = execution.getGroupId() + ":" + execution.getArtifactId();
        Plugin active = project.getBuild().getPluginsAsMap().get(key);
        Plugin managed = project.getPluginManagement() == null ? null : project.getPluginManagement().getPluginsAsMap().get(key);
        for (InputLocationTracker decl : Arrays.asList(
                active == null ? null : active.getExecutionsAsMap().get(execution.getExecutionId()),
                managed == null ? null : managed.getExecutionsAsMap().get(execution.getExecutionId()), active, managed)) {
            InputLocation loc = decl == null ? null : decl.getLocation("");
            if (loc == null || loc.getSource() == null || loc.getSource().getLocation() == null) continue;
            File pom = new File(loc.getSource().getLocation()).getCanonicalFile();
            if (!pom.isFile()) continue;
            if (!cache.containsKey(pom)) cache.put(pom, read(pom));
            Map<String, Directive> directives = cache.get(pom);
            Directive d = directives.get(loc.getLineNumber() + ":" + loc.getColumnNumber());
            if (d == null) d = directives.get(loc.getLineNumber() + ":" + (loc.getColumnNumber() + 1));
            if (d != null) return d;
        }
        return null;
    }

    private static Map<String, Directive> read(File file) throws Exception {
        XMLInputFactory xml = XMLInputFactory.newFactory();
        xml.setProperty(XMLInputFactory.SUPPORT_DTD, false);
        xml.setProperty(XMLInputFactory.IS_SUPPORTING_EXTERNAL_ENTITIES, false);
        Map<String, Directive> actions = new HashMap<>();
        try (InputStream in = new BufferedInputStream(new FileInputStream(file))) {
            XMLStreamReader r = xml.createXMLStreamReader(in);
            Deque<String> stack = new ArrayDeque<>();
            while (r.hasNext()) {
                int event = r.next();
                if (event == XMLStreamConstants.START_ELEMENT) {
                    Location loc = r.getLocation();
                    stack.push(("execution".equals(r.getLocalName()) || "plugin".equals(r.getLocalName()))
                            ? loc.getLineNumber() + ":" + loc.getColumnNumber() : "");
                } else if (event == XMLStreamConstants.END_ELEMENT) stack.pop();
                else if (event == XMLStreamConstants.PROCESSING_INSTRUCTION && "m2e".equals(r.getPITarget()) &&
                         !stack.isEmpty() && !stack.peek().isEmpty()) {
                    String data = r.getPIData();
                    if (data == null) continue;
                    String[] words = data.trim().split("\\s+", 2);
                    if ("ignore".equals(words[0]) && words.length == 1) actions.put(stack.peek(), new Directive(file,true,false));
                    else if ("execute".equals(words[0])) {
                        List<String> options = words.length == 1 ? Collections.emptyList() : Arrays.asList(words[1].split("\\s*,\\s*"));
                        if (Arrays.asList("onConfiguration", "onIncremental").containsAll(options))
                            actions.put(stack.peek(), new Directive(file,false,options.contains("onIncremental")));
                    }
                }
            }
            r.close();
        }
        return actions;
    }
}
