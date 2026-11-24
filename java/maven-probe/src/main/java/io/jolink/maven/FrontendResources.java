package io.jolink.maven;

import java.io.File;
import java.io.IOException;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.*;
import org.apache.maven.execution.MavenSession;
import org.apache.maven.model.Plugin;
import org.apache.maven.model.PluginExecution;
import org.apache.maven.model.Resource;
import org.apache.maven.plugin.*;
import org.apache.maven.project.MavenProject;
import org.codehaus.plexus.util.xml.Xpp3Dom;

/** Build declared npm assets in a private work directory, not in a user's source tree. */
final class FrontendResources implements AutoCloseable {
    private static final String FRONTEND = "com.github.eirslett:frontend-maven-plugin";
    private static final String RESOURCES = "org.apache.maven.plugins:maven-resources-plugin";
    final Path working, base, toolHome;
    final Set<Path> excluded = new LinkedHashSet<>();
    final Set<String> copies = new HashSet<>();
    final Set<String> builds = new HashSet<>();

    private FrontendResources(Path working) throws IOException {
        this.working = working;
        base = Files.createTempDirectory("jolink-frontend-");
        toolHome = base.resolve("toolchain");
    }

    static FrontendResources discover(MavenProject p, MavenSession s, ResourcePreparation resources) throws Exception {
        Plugin front = p.getPlugin(FRONTEND), copy = p.getPlugin(RESOURCES);
        if (front == null || copy == null) return null;
        Set<String> builds = new LinkedHashSet<>(), copies = new LinkedHashSet<>();
        Set<Path> destinations = new LinkedHashSet<>();
        Path working = null;
        for (PluginExecution e : front.getExecutions()) {
            if (!"compile".equals(e.getPhase()) || !e.getGoals().contains("npm")) continue;
            Xpp3Dom c = p.getGoalConfiguration(front.getGroupId(), front.getArtifactId(), e.getId(), "npm");
            PluginParameterExpressionEvaluator eval = new PluginParameterExpressionEvaluator(s,new MojoExecution(front,"npm",e.getId()));
            String args = text(c == null ? null : c.getChild("arguments"),eval);
            if (!"run build".equals(args) && !"run-script build".equals(args)) continue;
            Path w = resolve(text(c.getChild("workingDirectory"),eval),p.getBasedir());
            if (!w.startsWith(p.getBasedir().getCanonicalFile().toPath()) ||
                    (working != null && !working.equals(w))) return null;
            working = w; builds.add(e.getId());
        }
        if (working == null || !Files.isRegularFile(working.resolve("package.json"))) return null;
        Path classes = new File(p.getBuild().getOutputDirectory()).getCanonicalFile().toPath();
        for (PluginExecution e : copy.getExecutions()) {
            if (!"prepare-package".equals(e.getPhase()) || !e.getGoals().contains("copy-resources")) continue;
            Xpp3Dom c=p.getGoalConfiguration(copy.getGroupId(),copy.getArtifactId(),e.getId(),"copy-resources");
            PluginParameterExpressionEvaluator eval=new PluginParameterExpressionEvaluator(s,new MojoExecution(copy,"copy-resources",e.getId()));
            if (c==null || c.getChild("resources")==null || c.getChild("outputDirectory")==null ||
                    !resolve(text(c.getChild("outputDirectory"),eval),p.getBasedir()).startsWith(classes)) continue;
            Set<Path> inputs=new LinkedHashSet<>();
            for (Xpp3Dom r : c.getChild("resources").getChildren()) {
                Path path=resolve(text(r.getChild("directory"),eval),p.getBasedir());
                if (!path.startsWith(working) || path.equals(working)) { inputs.clear();break; }
                inputs.add(path);
            }
            if (!inputs.isEmpty()) { destinations.addAll(inputs);copies.add(e.getId()); }
        }
        if (copies.isEmpty()) return null;
        FrontendResources result=new FrontendResources(working);
        try {
            result.copies.addAll(copies); result.builds.addAll(builds);
            result.excluded.addAll(destinations);
            result.excluded.add(working.resolve(".git")); result.excluded.add(working.resolve("node_modules"));
            Path buildDir=new File(p.getBuild().getDirectory()).getCanonicalFile().toPath();
            if (buildDir.startsWith(working) && !buildDir.equals(working)) result.excluded.add(buildDir);
            result.stage(resources);
            resources.separateConsumerOutputs();
            return result;
        } catch(Exception failure) {result.close();throw failure;}
    }

    private static String text(Xpp3Dom node, PluginParameterExpressionEvaluator eval) throws Exception {
        if (node==null || node.getValue()==null) return null;
        Object v=eval.evaluate(node.getValue());
        return v==null ? null : v.toString().trim();
    }
    private static Path resolve(String v, File p) throws IOException {
        return p.toPath().resolve(v==null ? "." : v).toFile().getCanonicalFile().toPath();
    }
    private Path privateWork() {return base.resolve("work");}
    private void stage(ResourcePreparation resources) throws IOException {
        Resource resource=new Resource(); resource.setDirectory(working.toString());
        for(Path path:excluded) resource.addExclude(working.relativize(path).toString().replace(File.separatorChar,'/')+"/**");
        resources.addSelection(resource);
        Files.walkFileTree(working, new SimpleFileVisitor<Path>(){
            public FileVisitResult preVisitDirectory(Path dir, BasicFileAttributes a) throws IOException {
                if (excluded.contains(dir)) return FileVisitResult.SKIP_SUBTREE;
                Files.createDirectories(privateWork().resolve(working.relativize(dir))); return FileVisitResult.CONTINUE;
            }
            public FileVisitResult visitFile(Path file,BasicFileAttributes a) throws IOException {
                if (excluded.contains(file)) return FileVisitResult.CONTINUE;
                if (!a.isRegularFile()) throw new IOException("Unsupported non-regular frontend source: "+file);
                Files.copy(file,privateWork().resolve(working.relativize(file)),StandardCopyOption.COPY_ATTRIBUTES);
                return FileVisitResult.CONTINUE;
            }
        });
    }
    boolean builds(MojoExecution e) {return FRONTEND.equals(e.getGroupId()+":"+e.getArtifactId()) &&
            "npm".equals(e.getGoal()) && builds.contains(e.getExecutionId());}
    boolean copies(MojoExecution e) {return RESOURCES.equals(e.getGroupId()+":"+e.getArtifactId()) &&
            "copy-resources".equals(e.getGoal()) && copies.contains(e.getExecutionId());}
    boolean frontend(MojoExecution e) {return FRONTEND.equals(e.getGroupId()+":"+e.getArtifactId());}
    void execute(MavenSession s,MavenProject p,MojoExecution e,BuildPluginManager manager,ResourcePreparation resources) throws Exception {
        Xpp3Dom original=e.getConfiguration(), c=new Xpp3Dom(original);
        PluginParameterExpressionEvaluator eval=new PluginParameterExpressionEvaluator(s,e);
        if (copies(e)) {
            for (Xpp3Dom r:c.getChild("resources").getChildren()) {
                Xpp3Dom dir=r.getChild("directory");
                Path from=privateWork().resolve(working.relativize(resolve(text(dir,eval),p.getBasedir())));
                if (!Files.isDirectory(from)) throw new MojoExecutionException("Frontend build did not produce resource directory "+dir.getValue());
                dir.setValue(from.toString());
            }
        } else {
            Xpp3Dom dir=c.getChild("workingDirectory");
            if (dir!=null && !resolve(text(dir,eval),p.getBasedir()).equals(working))
                throw new MojoExecutionException("Frontend executions use different work directories.");
            set(c,"workingDirectory",privateWork()); set(c,"installDirectory",toolHome);
        }
        try {
            e.setConfiguration(c);
            if (copies(e)) resources.execute(s,p,e,manager,false); else manager.executeMojo(s,e);
        } finally {e.setConfiguration(original);}
    }
    private static void set(Xpp3Dom parent,String name,Path path) {
        Xpp3Dom field=parent.getChild(name);
        if (field==null) {field=new Xpp3Dom(name);parent.addChild(field);}
        field.setValue(path.toString());
    }
    public void close() throws IOException {
        Files.walkFileTree(base,new SimpleFileVisitor<Path>(){
            public FileVisitResult visitFile(Path f,BasicFileAttributes a)throws IOException {Files.delete(f);return FileVisitResult.CONTINUE;}
            public FileVisitResult postVisitDirectory(Path d,IOException e)throws IOException {if(e!=null)throw e;Files.delete(d);return FileVisitResult.CONTINUE;}
        });
    }
}
